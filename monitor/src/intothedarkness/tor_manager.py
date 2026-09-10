"""A Tor the tool carries with it.

Depending on a desktop Tor Browser, or a system package, or Docker, all make the
tool less portable than the thing it monitors. Instead this downloads the Tor
Project's own Expert Bundle — the same standalone tor that ships inside Tor
Browser — into the data directory and runs it as a managed child process on
ports of our choosing.

The result is `itd tor install && itd tor up` on any supported platform, with no
root, no package manager, and nothing left behind outside ``data/``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import tarfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from queue import Empty, Queue

import httpx

from .config import Settings, get_settings

log = logging.getLogger(__name__)

ARCHIVE_BASE = "https://archive.torproject.org/tor-package-archive/torbrowser"
VERSION_FEED = "https://aus1.torproject.org/torbrowser/update_3/release/downloads.json"
FALLBACK_VERSION = "15.0.21"

BOOTSTRAP_RE = re.compile(r"Bootstrapped (\d+)%(?: \(([^)]*)\))?(?::\s*(.*))?")

# Tor Browser's built-in bridges, shipped inside the Expert Bundle at
# tor/pluggable_transports/pt_config.json. Public and well known, so a censor
# that actively blocks Tor will likely block these too — but they are the right
# first thing to try on a network that *throttles* rather than blocks.
#
# meek_lite tunnels Tor inside ordinary HTTPS to a CDN. On networks where normal
# web traffic is fine but sustained relay TLS is starved, it is usually the only
# transport that gets through — verified to bootstrap where direct Tor could not.
BRIDGE_PRESETS = ("meek", "snowflake", "obfs4")


class TorInstallError(RuntimeError):
    """The bundled Tor could not be fetched, verified, or unpacked."""


class TorLaunchError(RuntimeError):
    """A managed Tor could not be started or did not bootstrap."""


def platform_slug() -> str:
    """The Expert Bundle name for this machine."""
    system = platform.system().lower()
    machine = platform.machine().lower()

    if system == "linux":
        arch = {"x86_64": "x86_64", "amd64": "x86_64", "i386": "i686", "i686": "i686"}.get(machine)
        if arch is None:
            raise TorInstallError(
                f"no Tor Expert Bundle for linux/{machine}. "
                "Install tor from your package manager and set ITD_TOR_BINARY."
            )
        return f"linux-{arch}"
    if system == "darwin":
        return "macos-aarch64" if machine in ("arm64", "aarch64") else "macos-x86_64"
    if system == "windows":
        return "windows-x86_64" if machine in ("amd64", "x86_64") else "windows-i686"

    raise TorInstallError(f"unsupported platform {system}/{machine}")


def latest_version(timeout: float = 30.0) -> str:
    """Ask Tor Project which release is current, falling back to a known one."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            payload = client.get(VERSION_FEED).json()
        for entry in (payload.get("downloads") or {}).values():
            binary = (entry.get("ALL") or {}).get("binary", "")
            match = re.search(r"/torbrowser/([0-9][0-9.]*)/", binary)
            if match:
                return match.group(1)
    except Exception as exc:
        log.debug("could not determine latest Tor version: %s", exc)
    return FALLBACK_VERSION


def bundle_url(version: str, slug: str) -> str:
    return f"{ARCHIVE_BASE}/{version}/tor-expert-bundle-{slug}-{version}.tar.gz"


def checksums_url(version: str) -> str:
    return f"{ARCHIVE_BASE}/{version}/sha256sums-unsigned-build.txt"


def published_sha256(version: str, filename: str, timeout: float = 60.0) -> str | None:
    """The digest Tor Project publishes for this exact file."""
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            text = client.get(checksums_url(version)).text
    except Exception as exc:
        log.debug("could not fetch checksums: %s", exc)
        return None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].strip() == filename:
            return parts[0].strip().lower()
    return None


@dataclass
class Install:
    root: Path
    binary: Path
    version: str
    slug: str
    verified: bool
    pluggable_transports: Path | None = None

    def env(self) -> dict[str, str]:
        """Environment a launched tor needs to find its bundled libraries."""
        env = dict(os.environ)
        lib = str(self.binary.parent)
        if platform.system().lower() == "darwin":
            env["DYLD_LIBRARY_PATH"] = lib + os.pathsep + env.get("DYLD_LIBRARY_PATH", "")
        else:
            env["LD_LIBRARY_PATH"] = lib + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        return env


def bundled_bridges(settings: Settings | None = None, kind: str = "meek") -> list[str]:
    """Read Tor Browser's default bridges out of the installed Expert Bundle."""
    import json

    local = installed(settings)
    if local is None or local.pluggable_transports is None:
        return []
    config = local.pluggable_transports / "pt_config.json"
    if not config.is_file():
        return []
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    bridges = (data.get("bridges") or {}).get(kind) or []
    return [str(b) for b in bridges if isinstance(b, str)]


def install_root(settings: Settings | None = None) -> Path:
    settings = settings or get_settings()
    return settings.data_dir / "tor"


def installed(settings: Settings | None = None) -> Install | None:
    """An Expert Bundle already unpacked in the data directory, if any."""
    root = install_root(settings)
    exe = "tor.exe" if platform.system().lower() == "windows" else "tor"
    binary = root / "tor" / exe
    if not binary.is_file():
        return None
    version = (root / "VERSION").read_text().strip() if (root / "VERSION").is_file() else "unknown"
    pt = root / "tor" / "pluggable_transports"
    return Install(
        root=root,
        binary=binary,
        version=version,
        slug=platform_slug(),
        verified=(root / "VERIFIED").is_file(),
        pluggable_transports=pt if pt.is_dir() else None,
    )


def resolve_binary(settings: Settings | None = None) -> tuple[Path, str] | None:
    """Find a tor binary: explicit setting, then vendored, then PATH."""
    settings = settings or get_settings()

    if settings.tor_binary:
        candidate = Path(settings.tor_binary).expanduser()
        if candidate.is_file():
            return candidate, "ITD_TOR_BINARY"

    local = installed(settings)
    if local is not None:
        return local.binary, f"bundled {local.version}"

    found = shutil.which("tor")
    if found:
        return Path(found), "system PATH"
    return None


def install(
    settings: Settings | None = None,
    version: str | None = None,
    force: bool = False,
    progress=None,
) -> Install:
    """Download, verify and unpack the Tor Expert Bundle into ``data/tor``."""
    settings = settings or get_settings()
    settings.ensure_dirs()
    root = install_root(settings)

    existing = installed(settings)
    if existing is not None and not force:
        return existing

    slug = platform_slug()
    version = version or latest_version()
    url = bundle_url(version, slug)
    filename = url.rsplit("/", 1)[-1]

    if progress:
        progress(f"downloading {filename}")

    root.mkdir(parents=True, exist_ok=True)
    archive = root / filename
    digest = hashlib.sha256()
    try:
        with (
            httpx.Client(timeout=120.0, follow_redirects=True) as client,
            client.stream("GET", url) as response,
        ):
            if response.status_code != 200:
                raise TorInstallError(f"{url} returned HTTP {response.status_code}")
            with archive.open("wb") as handle:
                for chunk in response.iter_bytes(chunk_size=262144):
                    handle.write(chunk)
                    digest.update(chunk)
    except httpx.HTTPError as exc:
        raise TorInstallError(f"could not download {url}: {exc}") from exc

    actual = digest.hexdigest()
    expected = published_sha256(version, filename)
    verified = bool(expected) and actual == expected
    if expected and not verified:
        archive.unlink(missing_ok=True)
        raise TorInstallError(
            f"checksum mismatch for {filename}: expected {expected}, got {actual}"
        )
    if progress:
        progress(
            f"sha256 {actual[:16]}… "
            + ("verified against Tor Project's published sums" if verified else "UNVERIFIED")
        )

    if progress:
        progress("extracting")
    for stale in ("tor", "data", "debug", "docs"):
        shutil.rmtree(root / stale, ignore_errors=True)

    with tarfile.open(archive, "r:gz") as tar:
        # Refuse absolute paths and traversal before writing anything.
        for member in tar.getmembers():
            target = (root / member.name).resolve()
            if not str(target).startswith(str(root.resolve())):
                raise TorInstallError(f"archive contains an unsafe path: {member.name}")
        # filter="data" (3.12+) blocks absolute paths, traversal, links and
        # device nodes on its own; the loop above is belt and braces.
        try:
            tar.extractall(root, filter="data")
        except TypeError:  # Python < 3.12
            tar.extractall(root)  # noqa: S202 - members validated above

    archive.unlink(missing_ok=True)
    (root / "VERSION").write_text(version, encoding="utf-8")
    if verified:
        (root / "VERIFIED").write_text(actual, encoding="utf-8")

    result = installed(settings)
    if result is None:
        raise TorInstallError(f"unpacked {filename} but found no tor binary under {root}")
    result.binary.chmod(0o755)
    if result.pluggable_transports:
        for pt in result.pluggable_transports.iterdir():
            if pt.is_file() and not pt.suffix:
                pt.chmod(0o755)
    return result


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def port_open(port: int, host: str = "127.0.0.1", timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@dataclass
class ManagedTor:
    """A tor process this tool owns: its ports, its data directory, its lifetime."""

    settings: Settings = field(default_factory=get_settings)
    socks_port: int | None = None
    control_port: int | None = None
    process: subprocess.Popen | None = None

    @property
    def state_dir(self) -> Path:
        path = self.settings.data_dir / "tor-run"
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "tor.pid"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "tor.log"

    @property
    def torrc(self) -> Path:
        return self.state_dir / "torrc"

    # --------------------------------------------------------------- lifecycle

    def running_pid(self) -> int | None:
        """The PID of a tor we previously started, if it is still alive."""
        if not self.pid_file.is_file():
            return None
        try:
            pid = int(self.pid_file.read_text().split()[0])
        except (ValueError, IndexError, OSError):
            return None
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return pid

    def running_ports(self) -> tuple[int, int] | None:
        if not self.pid_file.is_file() or self.running_pid() is None:
            return None
        parts = self.pid_file.read_text().split()
        if len(parts) >= 3:
            return int(parts[1]), int(parts[2])
        return None

    def write_torrc(self, socks_port: int, control_port: int, install: Install) -> Path:
        data_dir = self.state_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        data_dir.chmod(0o700)

        lines = [
            "# Generated by `itd tor up`. Edits here are overwritten.",
            f"SocksPort 127.0.0.1:{socks_port}",
            f"ControlPort 127.0.0.1:{control_port}",
            "CookieAuthentication 1",
            f"DataDirectory {data_dir}",
            "ClientOnly 1",
            "ExitPolicy reject *:*",
            "CircuitStreamTimeout 120",
            "SocksTimeout 120",
        ]
        geoip = install.root / "data" / "geoip"
        geoip6 = install.root / "data" / "geoip6"
        if geoip.is_file():
            lines.append(f"GeoIPFile {geoip}")
        if geoip6.is_file():
            lines.append(f"GeoIPv6File {geoip6}")

        bridges = [b.strip() for b in self.settings.tor_bridges if b.strip()]
        if bridges:
            # lyrebird is the obfs4 transport shipped inside the Expert Bundle,
            # so bridges work without installing anything else.
            lyrebird = None
            if install.pluggable_transports:
                for name in ("lyrebird", "obfs4proxy"):
                    candidate = install.pluggable_transports / name
                    if candidate.is_file():
                        lyrebird = candidate
                        break
            if lyrebird is not None:
                lines.append("UseBridges 1")
                lines.append(
                    "ClientTransportPlugin "
                    f"meek_lite,obfs2,obfs3,obfs4,scramblesuit,webtunnel exec {lyrebird}"
                )
                # snowflake is served by the same binary under a different name.
                if any(b.split()[0] == "snowflake" for b in bridges):
                    lines.append(f"ClientTransportPlugin snowflake exec {lyrebird}")
                lines.extend(f"Bridge {bridge}" for bridge in bridges)
            else:
                log.warning("bridges configured but no pluggable transport binary was found")

        self.torrc.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return self.torrc

    def start(
        self,
        timeout: float = 180.0,
        progress=None,
        socks_port: int | None = None,
        control_port: int | None = None,
    ) -> tuple[int, int]:
        """Launch tor and wait for it to bootstrap. Returns (socks, control)."""
        existing = self.running_ports()
        if existing is not None:
            self.socks_port, self.control_port = existing
            return existing

        found = resolve_binary(self.settings)
        if found is None:
            raise TorLaunchError(
                "no tor binary available — run `itd tor install` to fetch one"
            )
        binary, _origin = found

        bundle = installed(self.settings)
        if bundle is None:
            # A system tor still works; it just has no bundled libraries.
            bundle = Install(
                root=install_root(self.settings), binary=binary,
                version="system", slug="system", verified=False,
            )

        self.socks_port = socks_port or free_port()
        self.control_port = control_port or free_port()
        self.write_torrc(self.socks_port, self.control_port, bundle)

        log_handle = self.log_file.open("w", encoding="utf-8")
        try:
            self.process = subprocess.Popen(
                [str(binary), "-f", str(self.torrc)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=bundle.env(),
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            log_handle.close()
            raise TorLaunchError(f"could not start {binary}: {exc}") from exc

        # tor can go quiet for minutes on a throttled network, and a blocking
        # readline() would ignore the deadline entirely. Read on a thread and
        # poll with a timeout so the caller's timeout actually means something.
        lines: Queue[str | None] = Queue()

        def pump(stream) -> None:
            try:
                for line in iter(stream.readline, ""):
                    lines.put(line)
            finally:
                lines.put(None)

        reader = threading.Thread(target=pump, args=(self.process.stdout,), daemon=True)
        reader.start()

        deadline = time.monotonic() + timeout
        percent = 0

        try:
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=1.0)
                except Empty:
                    if self.process.poll() is not None:
                        raise TorLaunchError(
                            f"tor exited with code {self.process.returncode}; "
                            f"see {self.log_file}"
                        ) from None
                    continue

                if line is None:  # stdout closed
                    if self.process.poll() is not None:
                        raise TorLaunchError(
                            f"tor exited with code {self.process.returncode}; "
                            f"see {self.log_file}"
                        )
                    break

                log_handle.write(line)
                log_handle.flush()

                match = BOOTSTRAP_RE.search(line)
                if match:
                    percent = int(match.group(1))
                    detail = match.group(3) or match.group(2) or ""
                    if progress:
                        progress(percent, detail.strip())
                    if percent >= 100:
                        self.pid_file.write_text(
                            f"{self.process.pid} {self.socks_port} {self.control_port}\n"
                        )
                        return self.socks_port, self.control_port
        finally:
            log_handle.close()

        self.stop()
        raise TorLaunchError(
            f"tor reached {percent}% in {timeout:.0f}s and gave up. "
            "Stalling below 25% usually means the network blocks or throttles "
            "connections to Tor relays — configure bridges (ITD_TOR_BRIDGES)."
        )

    def stop(self) -> bool:
        """Stop a tor we started. Returns whether anything was stopped."""
        stopped = False

        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self.process.kill()
            stopped = True
            self.process = None

        pid = self.running_pid()
        if pid is not None:
            try:
                os.kill(pid, signal.SIGTERM)
                for _ in range(30):
                    time.sleep(0.5)
                    try:
                        os.kill(pid, 0)
                    except OSError:
                        break
                else:
                    os.kill(pid, signal.SIGKILL)
                stopped = True
            except OSError:
                pass

        self.pid_file.unlink(missing_ok=True)
        return stopped

    def socks_url(self) -> str | None:
        ports = self.running_ports()
        if ports is None:
            return None
        return f"socks5://127.0.0.1:{ports[0]}"
