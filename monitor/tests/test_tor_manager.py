"""The bundled Tor: platform resolution, verified install, managed process."""

from __future__ import annotations

import hashlib
import io
import tarfile

import pytest

from intothedarkness import tor_manager as tm


def test_platform_slug_matches_a_published_bundle():
    slug = tm.platform_slug()
    assert slug in {
        "linux-x86_64", "linux-i686",
        "macos-x86_64", "macos-aarch64",
        "windows-x86_64", "windows-i686",
    }


def test_bundle_and_checksum_urls():
    url = tm.bundle_url("15.0.21", "linux-x86_64")
    assert url.endswith("/15.0.21/tor-expert-bundle-linux-x86_64-15.0.21.tar.gz")
    assert tm.checksums_url("15.0.21").endswith("/15.0.21/sha256sums-unsigned-build.txt")


def test_unsupported_platform_says_what_to_do(monkeypatch):
    monkeypatch.setattr(tm.platform, "system", lambda: "Linux")
    monkeypatch.setattr(tm.platform, "machine", lambda: "sparc64")
    with pytest.raises(tm.TorInstallError, match="ITD_TOR_BINARY"):
        tm.platform_slug()


def test_binary_resolution_prefers_the_explicit_setting(settings, tmp_path):
    fake = tmp_path / "mytor"
    fake.write_text("#!/bin/sh\n")
    settings.tor_binary = str(fake)
    resolved, origin = tm.resolve_binary(settings)
    assert resolved == fake and origin == "ITD_TOR_BINARY"


def test_binary_resolution_falls_back_to_path(settings, monkeypatch):
    settings.tor_binary = ""
    monkeypatch.setattr(tm.shutil, "which", lambda name: "/usr/bin/tor")
    resolved, origin = tm.resolve_binary(settings)
    assert str(resolved) == "/usr/bin/tor" and origin == "system PATH"


def test_no_binary_anywhere_resolves_to_none(settings, monkeypatch):
    settings.tor_binary = ""
    monkeypatch.setattr(tm.shutil, "which", lambda name: None)
    assert tm.resolve_binary(settings) is None


def test_installed_returns_none_before_installing(settings):
    settings.ensure_dirs()
    assert tm.installed(settings) is None


# ------------------------------------------------------------------- install


def _bundle_bytes() -> bytes:
    """A miniature Expert Bundle with the layout the real one has."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, body in (
            ("tor/tor", b"#!/bin/sh\necho tor\n"),
            ("tor/pluggable_transports/lyrebird", b"#!/bin/sh\n"),
            ("data/geoip", b"# geoip\n"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(body)
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(body))
    return buffer.getvalue()


class _FakeStream:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self.payload, self.status_code = payload, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_bytes(self, chunk_size=65536):
        yield self.payload


class _FakeClient:
    """Serves the bundle and its checksum file without touching the network."""

    def __init__(self, payload: bytes, digest: str | None, status: int = 200) -> None:
        self.payload, self.digest, self.status = payload, digest, status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def stream(self, method, url):
        return _FakeStream(self.payload, self.status)

    def get(self, url):
        name = "tor-expert-bundle-linux-x86_64-9.9.9.tar.gz"
        text = f"{self.digest}  {name}\n" if self.digest else ""

        class _Resp:
            def __init__(self, text):
                self.text = text

        return _Resp(text)


def _patch_http(monkeypatch, payload, digest, status=200):
    monkeypatch.setattr(tm, "platform_slug", lambda: "linux-x86_64")
    monkeypatch.setattr(tm, "latest_version", lambda timeout=30.0: "9.9.9")
    monkeypatch.setattr(
        tm.httpx, "Client", lambda **kw: _FakeClient(payload, digest, status)
    )


def test_install_verifies_the_published_checksum(settings, monkeypatch):
    payload = _bundle_bytes()
    _patch_http(monkeypatch, payload, hashlib.sha256(payload).hexdigest())

    result = tm.install(settings)
    assert result.verified
    assert result.binary.is_file()
    assert result.pluggable_transports is not None
    assert (result.root / "VERSION").read_text() == "9.9.9"


def test_install_refuses_a_checksum_mismatch(settings, monkeypatch):
    _patch_http(monkeypatch, _bundle_bytes(), "0" * 64)
    with pytest.raises(tm.TorInstallError, match="checksum mismatch"):
        tm.install(settings)
    assert tm.installed(settings) is None


def test_install_proceeds_unverified_when_checksums_are_unreachable(settings, monkeypatch):
    _patch_http(monkeypatch, _bundle_bytes(), None)
    result = tm.install(settings)
    assert result.binary.is_file()
    assert not result.verified          # recorded honestly rather than assumed


def test_install_rejects_path_traversal_in_the_archive(settings, monkeypatch):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        info = tarfile.TarInfo("../../escaped")
        info.size = 3
        tar.addfile(info, io.BytesIO(b"bad"))
    payload = buffer.getvalue()
    _patch_http(monkeypatch, payload, hashlib.sha256(payload).hexdigest())

    with pytest.raises(tm.TorInstallError, match="unsafe path"):
        tm.install(settings)


def test_install_is_idempotent(settings, monkeypatch):
    payload = _bundle_bytes()
    _patch_http(monkeypatch, payload, hashlib.sha256(payload).hexdigest())
    first = tm.install(settings)
    second = tm.install(settings)
    assert first.binary == second.binary


def test_http_error_is_reported(settings, monkeypatch):
    _patch_http(monkeypatch, b"", None, status=404)
    with pytest.raises(tm.TorInstallError, match="HTTP 404"):
        tm.install(settings)


# ------------------------------------------------------------ managed process


def test_torrc_is_generated_with_our_ports_and_data_dir(settings, monkeypatch):
    payload = _bundle_bytes()
    _patch_http(monkeypatch, payload, hashlib.sha256(payload).hexdigest())
    install = tm.install(settings)

    managed = tm.ManagedTor(settings=settings)
    path = managed.write_torrc(19050, 19051, install)
    text = path.read_text()

    assert "SocksPort 127.0.0.1:19050" in text
    assert "ControlPort 127.0.0.1:19051" in text
    assert "ClientOnly 1" in text
    assert "CookieAuthentication 1" in text
    assert "GeoIPFile" in text
    assert "UseBridges" not in text      # none configured


def test_bridges_wire_up_the_bundled_transport(settings, monkeypatch):
    payload = _bundle_bytes()
    _patch_http(monkeypatch, payload, hashlib.sha256(payload).hexdigest())
    install = tm.install(settings)

    settings.tor_bridges = ["obfs4 1.2.3.4:443 ABC cert=xyz iat-mode=0"]
    managed = tm.ManagedTor(settings=settings)
    text = managed.write_torrc(19050, 19051, install).read_text()

    assert "UseBridges 1" in text
    assert "lyrebird" in text            # shipped inside the Expert Bundle
    assert "Bridge obfs4 1.2.3.4:443" in text


def test_free_port_is_actually_free():
    port = tm.free_port()
    assert 1024 < port < 65536
    assert not tm.port_open(port, timeout=0.5)


def test_running_pid_is_none_without_a_pid_file(settings):
    assert tm.ManagedTor(settings=settings).running_pid() is None
    assert tm.ManagedTor(settings=settings).running_ports() is None
    assert tm.ManagedTor(settings=settings).socks_url() is None


def test_stale_pid_file_is_not_mistaken_for_a_running_tor(settings):
    managed = tm.ManagedTor(settings=settings)
    managed.pid_file.write_text("999999 9050 9051\n")   # a PID that cannot exist
    assert managed.running_pid() is None
    assert managed.running_ports() is None


def test_start_without_a_binary_says_what_to_run(settings, monkeypatch):
    settings.tor_binary = ""
    monkeypatch.setattr(tm, "resolve_binary", lambda s=None: None)
    with pytest.raises(tm.TorLaunchError, match="itd tor install"):
        tm.ManagedTor(settings=settings).start(timeout=1.0)


def test_bootstrap_timeout_is_honoured_when_tor_goes_quiet(settings, monkeypatch, tmp_path):
    """A silent tor must not block past the deadline — readline() would.

    This is the regression: a blocking read ignored the caller's timeout
    entirely on a throttled network, where tor emits nothing for minutes.
    """
    import time

    quiet = tmp_path / "quiet.sh"
    quiet.write_text("#!/bin/sh\necho 'Bootstrapped 10% (conn): Connected'\nsleep 60\n")
    quiet.chmod(0o755)
    monkeypatch.setattr(tm, "resolve_binary", lambda s=None: (quiet, "test"))

    managed = tm.ManagedTor(settings=settings)
    started = time.monotonic()
    with pytest.raises(tm.TorLaunchError, match="10%"):
        managed.start(timeout=3.0)
    elapsed = time.monotonic() - started

    assert elapsed < 12.0, f"timeout not honoured: took {elapsed:.1f}s"
    managed.stop()


def test_stop_reports_when_nothing_was_running(settings):
    assert tm.ManagedTor(settings=settings).stop() is False
