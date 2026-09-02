"""Tor specifics: onion addresses, log redaction, and optional circuit control.

Nothing here starts or manages a Tor daemon. Bring your own — a system service
or a container — and point ``tor_socks_url`` at its SOCKS port. Embedding Tor
means owning its packaging and security updates, which is not a job this tool
should take on.
"""

from __future__ import annotations

import contextlib
import logging
import re
import threading
import time
from dataclasses import dataclass
from urllib.parse import urlparse

from .config import Settings, get_settings

log = logging.getLogger(__name__)

# A v3 onion address is 56 characters of base32 (a-z, 2-7) plus ".onion".
# v2 addresses were 16 characters and have been unroutable since Tor 0.4.6.
ONION_V3_RE = re.compile(r"^[a-z2-7]{56}\.onion$", re.I)
ONION_V2_RE = re.compile(r"^[a-z2-7]{16}\.onion$", re.I)
ONION_ANY_RE = re.compile(r"\b[a-z2-7]{16}(?:[a-z2-7]{40})?\.onion\b", re.I)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_onion(url: str) -> bool:
    """Whether a URL points at a Tor hidden service."""
    return host_of(url).endswith(".onion")


@dataclass(frozen=True)
class OnionCheck:
    ok: bool
    version: int | None
    reason: str


def validate_onion(url: str) -> OnionCheck:
    """Check an onion address's shape before spending 90 seconds on a timeout."""
    host = host_of(url)
    if not host.endswith(".onion"):
        return OnionCheck(False, None, f"{host!r} is not a .onion address")
    if ONION_V3_RE.match(host):
        return OnionCheck(True, 3, "valid v3 address")
    if ONION_V2_RE.match(host):
        return OnionCheck(
            False, 2, "v2 onion addresses were deprecated in Tor 0.4.6 and no longer resolve"
        )
    label = host[: -len(".onion")]
    return OnionCheck(
        False,
        None,
        f"malformed onion address: expected 56 base32 characters, got {len(label)}",
    )


def redact(text: str, enabled: bool = True) -> str:
    """Mask onion addresses so log shipping does not publish the target list."""
    if not enabled or not text:
        return text

    def _mask(match: re.Match[str]) -> str:
        value = match.group(0)
        return f"{value[:6]}…{value[-12:]}"

    return ONION_ANY_RE.sub(_mask, text)


class TorUnavailable(RuntimeError):
    """Tor is not reachable, or the control port is not usable."""


# Where a SOCKS proxy is usually found. A standalone tor daemon listens on
# 9050; Tor Browser's bundled tor listens on 9150 while the browser is open.
KNOWN_SOCKS_PORTS = ((9050, "tor daemon"), (9150, "Tor Browser"))

# SOCKS5 reply codes worth naming (RFC 1928 §6).
SOCKS_REPLIES = {
    0x00: "succeeded",
    0x01: "general SOCKS server failure",
    0x02: "connection not allowed by ruleset",
    0x03: "network unreachable",
    0x04: "host unreachable",
    0x05: "connection refused",
    0x06: "TTL expired",
    0x07: "command not supported",
    0x08: "address type not supported",
}


def check_socks(settings: Settings | None = None, timeout: float = 5.0) -> tuple[bool, str]:
    """Is something accepting connections on the SOCKS port?

    A plain TCP connect: enough to tell "no daemon" from "daemon is up", but not
    whether tor has built a circuit yet — use :func:`probe_socks` for that.
    """
    import socket

    settings = settings or get_settings()
    parsed = urlparse(settings.tor_socks_url)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 9050
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, f"SOCKS proxy reachable at {host}:{port}"
    except OSError as exc:
        return False, f"no SOCKS proxy at {host}:{port} ({exc.__class__.__name__}: {exc})"


@dataclass(frozen=True)
class SocksProbe:
    """What a SOCKS5 handshake plus CONNECT actually told us."""

    listening: bool
    usable: bool
    reply_code: int | None
    detail: str

    @property
    def bootstrapping(self) -> bool:
        """Port answers, but tor cannot yet route — almost always bootstrap."""
        return self.listening and not self.usable


def probe_socks(
    host: str = "127.0.0.1",
    port: int = 9050,
    through: str = "check.torproject.org",
    through_port: int = 443,
    timeout: float = 20.0,
) -> SocksProbe:
    """Speak SOCKS5 and ask to CONNECT, to tell "up" from "actually working".

    A tor that is still bootstrapping accepts connections on its SOCKS port and
    then fails the CONNECT, which looks identical to a healthy proxy if you only
    test the TCP connect. Distinguishing the two is the difference between
    "wait" and "fix your config".
    """
    import socket

    try:
        sock = socket.create_connection((host, port), timeout=timeout)
    except OSError as exc:
        return SocksProbe(False, False, None, f"no listener on {host}:{port} ({exc})")

    try:
        sock.settimeout(timeout)
        sock.sendall(bytes([0x05, 0x01, 0x00]))          # SOCKS5, one method, no auth
        greeting = sock.recv(2)
        if len(greeting) < 2 or greeting[0] != 0x05:
            return SocksProbe(True, False, None, "listener is not a SOCKS5 proxy")
        if greeting[1] != 0x00:
            return SocksProbe(True, False, None, "SOCKS5 proxy demands authentication")

        target = through.encode("ascii")
        request = bytes([0x05, 0x01, 0x00, 0x03, len(target)]) + target
        request += through_port.to_bytes(2, "big")
        sock.sendall(request)

        reply = sock.recv(4)
        if len(reply) < 2:
            return SocksProbe(True, False, None, "SOCKS5 proxy closed the connection")

        code = reply[1]
        if code == 0x00:
            return SocksProbe(True, True, code, f"circuit established via {host}:{port}")
        return SocksProbe(
            True, False, code, SOCKS_REPLIES.get(code, f"SOCKS error 0x{code:02x}")
        )
    except OSError as exc:
        return SocksProbe(True, False, None, f"SOCKS handshake failed: {exc}")
    finally:
        with contextlib.suppress(OSError):
            sock.close()


def find_socks(timeout: float = 8.0) -> list[tuple[int, str, SocksProbe]]:
    """Probe the ports a SOCKS proxy is conventionally found on."""
    out = []
    for port, label in KNOWN_SOCKS_PORTS:
        probe = probe_socks(port=port, timeout=timeout)
        if probe.listening:
            out.append((port, label, probe))
    return out


class TorController:
    """Optional wrapper over Tor's ControlPort for circuit rotation.

    ``stem`` is an optional dependency; without it (or without a ControlPort)
    every method degrades to a no-op and the fetcher simply retries as usual.
    Tor rate-limits NEWNYM, so requests to rotate faster than it allows are
    dropped rather than queued.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._lock = threading.Lock()
        self._last_rotate: float | None = None
        self._unavailable_reason: str | None = None

    def available(self) -> tuple[bool, str]:
        try:
            import stem  # noqa: F401
        except ImportError:
            return False, "stem is not installed (pip install 'intothedarkness[tor]')"
        if self._unavailable_reason:
            return False, self._unavailable_reason
        return True, ""

    def _throttled(self) -> float:
        """Seconds still to wait before our own rotation guard allows another."""
        if self._last_rotate is None:
            return 0.0
        elapsed = time.monotonic() - self._last_rotate
        return max(0.0, self.settings.tor_min_rotate_interval - elapsed)

    def new_circuit(self) -> tuple[bool, str]:
        """Ask Tor for a fresh circuit. Returns (rotated, explanation)."""
        ok, why = self.available()
        if not ok:
            return False, why

        with self._lock:
            wait = self._throttled()
            if wait > 0:
                return False, f"rotated {wait:.0f}s ago; holding off"

            from stem import Signal
            from stem.control import Controller

            try:
                with Controller.from_port(port=self.settings.tor_control_port) as controller:
                    if self.settings.tor_control_password:
                        controller.authenticate(password=self.settings.tor_control_password)
                    else:
                        controller.authenticate()

                    # Tor refuses NEWNYM more often than roughly every 10s.
                    pending = controller.get_newnym_wait()
                    if pending > 0:
                        return False, f"tor rate-limits NEWNYM for another {pending:.0f}s"

                    controller.signal(Signal.NEWNYM)
                    self._last_rotate = time.monotonic()
                    return True, "requested a new circuit"
            except Exception as exc:
                self._unavailable_reason = f"control port unusable: {exc}"
                return False, self._unavailable_reason

    def identity(self, timeout: float = 30.0) -> str | None:
        """The exit IP Tor currently presents, via a clearnet check service."""
        import httpx

        try:
            with httpx.Client(proxy=self.settings.tor_socks_url, timeout=timeout) as client:
                resp = client.get("https://check.torproject.org/api/ip")
                return resp.json().get("IP")
        except Exception as exc:
            log.debug("could not determine exit IP: %s", exc)
            return None
