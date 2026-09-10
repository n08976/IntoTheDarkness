"""HTTP fetching over clearnet or Tor.

One :class:`Fetcher` serves a whole sweep and keeps a small pool of clients,
one per *network profile*. A profile bundles everything that must not be shared
between networks: the proxy, TLS posture, timeouts, retry budget and cookie jar.
Requests are routed by URL — ``.onion`` goes through Tor — or by an explicit
per-target override.
"""

from __future__ import annotations

import logging
import threading
import time
import urllib.robotparser
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import urljoin, urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import Settings, get_settings
from ..tor import TorController, is_onion, redact, validate_onion

log = logging.getLogger(__name__)

RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

# Failures that suggest a bad circuit rather than a dead service.
CIRCUIT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.RemoteProtocolError,
    httpx.ProxyError,
)


class Network(StrEnum):
    """Which network a request travels over."""

    AUTO = "auto"      # decide from the URL: .onion via Tor, everything else direct
    DIRECT = "direct"  # clearnet, no proxy
    TOR = "tor"        # through the SOCKS proxy, including clearnet-over-Tor


class FetchError(RuntimeError):
    """A fetch failed in a way worth retrying or reporting."""


class TorNotConfigured(FetchError):
    """A request needed Tor, but Tor is disabled or unreachable."""


@dataclass(slots=True)
class Response:
    url: str
    status: int
    text: str
    content: bytes
    headers: dict[str, str]
    network: str = Network.DIRECT.value

    def json(self):
        import json

        return json.loads(self.text)


def resolve_network(url: str, requested: str | Network = Network.AUTO) -> Network:
    """Pick the network for a URL.

    An explicit override always wins, so a misconfigured ``.onion`` fails loudly
    as a Tor problem rather than silently leaking to a clearnet DNS lookup.
    """
    requested = Network(requested)
    if requested is not Network.AUTO:
        return requested
    return Network.TOR if is_onion(url) else Network.DIRECT


class RobotsCache:
    """Per-host robots.txt, fetched once and reused."""

    def __init__(self, user_agent: str) -> None:
        self.user_agent = user_agent
        self._cache: dict[str, urllib.robotparser.RobotFileParser | None] = {}
        self._lock = threading.Lock()

    def allowed(self, url: str, client: httpx.Client, timeout: float = 10.0) -> bool:
        parts = urlparse(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with self._lock:
            if origin not in self._cache:
                self._cache[origin] = self._load(origin, client, timeout)
            parser = self._cache[origin]
        if parser is None:
            return True  # no robots.txt, or unreadable: default to allowed
        return parser.can_fetch(self.user_agent, url)

    def _load(self, origin: str, client: httpx.Client, timeout: float):
        try:
            resp = client.get(urljoin(origin, "/robots.txt"), timeout=timeout)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200 or not resp.text.strip():
            return None
        parser = urllib.robotparser.RobotFileParser()
        parser.parse(resp.text.splitlines())
        return parser


@dataclass(slots=True)
class Profile:
    """Everything that differs between the networks we can fetch over."""

    network: Network
    proxy: str | None
    verify: bool
    timeout: float
    attempts: int
    respect_robots: bool
    delay: float

    @classmethod
    def build(cls, network: Network, settings: Settings) -> Profile:
        if network is Network.TOR:
            return cls(
                network=network,
                proxy=settings.tor_socks_url,
                # Onion services are self-authenticating and usually plain HTTP;
                # verifying by default only breaks fetches.
                verify=settings.onion_verify_tls,
                timeout=settings.tor_timeout,
                attempts=max(1, settings.tor_max_retries),
                # robots.txt on a hidden service is a latency and failure
                # amplifier, not a politeness contract anyone honours.
                respect_robots=False,
                delay=settings.tor_delay,
            )
        return cls(
            network=network,
            proxy=None,
            verify=settings.verify_tls,
            timeout=settings.request_timeout,
            attempts=max(1, settings.max_retries),
            respect_robots=settings.respect_robots,
            delay=settings.per_host_delay,
        )


class Fetcher:
    """A small HTTP client with the manners a scraper should have."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._clients: dict[Network, httpx.Client] = {}
        self._robots = RobotsCache(self.settings.user_agent)
        self._last_host_hit: dict[str, float] = {}
        self._last_network_hit: dict[Network, float] = {}
        self._lock = threading.Lock()
        self._client_lock = threading.Lock()
        self.tor = TorController(self.settings)

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        with self._client_lock:
            for client in self._clients.values():
                client.close()
            self._clients.clear()

    # ------------------------------------------------------------------ clients

    def profile(self, network: Network) -> Profile:
        return Profile.build(network, self.settings)

    def _client(self, network: Network) -> httpx.Client:
        """One client per network, created on demand and reused for the sweep.

        Clients are never reconfigured after creation: switching a live client's
        proxy would let pooled connections and cookies cross the network
        boundary.
        """
        with self._client_lock:
            client = self._clients.get(network)
            if client is not None:
                return client

            profile = self.profile(network)
            if network is Network.TOR:
                if not self.settings.tor_enabled:
                    raise TorNotConfigured(
                        "this request needs Tor but tor_enabled is false "
                        "(set ITD_TOR_ENABLED=true)"
                    )
                client = httpx.Client(
                    proxy=profile.proxy,
                    # Redirects are handled by us, not httpx, so a hop can never
                    # cross the Tor/clearnet boundary unnoticed.
                    follow_redirects=False,
                    timeout=profile.timeout,
                    verify=profile.verify,
                    headers={"User-Agent": self.settings.user_agent},
                )
            else:
                client = httpx.Client(
                    follow_redirects=False,
                    timeout=profile.timeout,
                    verify=profile.verify,
                    headers={"User-Agent": self.settings.user_agent},
                )
            self._clients[network] = client
            return client

    # --------------------------------------------------------------- throttling

    def _throttle(self, url: str, network: Network, profile: Profile) -> None:
        """Pace requests per host and, for Tor, across the whole network.

        Over Tor the bottleneck is the shared circuit rather than the remote
        service, so the network-wide delay applies even to unrelated hosts.
        """
        host = urlparse(url).netloc
        with self._lock:
            now = time.monotonic()
            waits = []

            last_host = self._last_host_hit.get(host)
            if last_host is not None and profile.delay > 0:
                waits.append(profile.delay - (now - last_host))

            if network is Network.TOR and self.settings.tor_delay > 0:
                last_net = self._last_network_hit.get(network)
                if last_net is not None:
                    waits.append(self.settings.tor_delay - (now - last_net))

            wait = max(waits) if waits else 0.0
            if wait > 0:
                time.sleep(wait)
                now = time.monotonic()

            self._last_host_hit[host] = now
            self._last_network_hit[network] = now

    # ------------------------------------------------------------------ fetching

    def _redact(self, text: str) -> str:
        return redact(text, self.settings.redact_onion_in_logs)

    def get(self, url: str, **kwargs) -> Response:
        return self.request("GET", url, **kwargs)

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        body: str | None = None,
        network: str | Network = Network.AUTO,
        max_redirects: int = 5,
    ) -> Response:
        """Fetch a URL, following redirects only within the same network."""
        resolved = resolve_network(url, network)

        if resolved is Network.TOR and is_onion(url):
            check = validate_onion(url)
            if not check.ok:
                raise FetchError(f"{self._redact(url)}: {check.reason}")

        current = url
        for hop in range(max_redirects + 1):
            resp = self._request_once(method, current, resolved, headers, params, body)

            if resp.status not in (301, 302, 303, 307, 308):
                return resp

            location = resp.headers.get("location")
            if not location:
                return resp

            target = urljoin(current, location)
            hop_network = resolve_network(target, network)
            if hop_network is not resolved:
                # A .onion redirecting to clearnet (or the reverse) changes the
                # threat model; refuse rather than silently switch networks.
                raise FetchError(
                    f"refusing redirect across networks: "
                    f"{self._redact(current)} ({resolved.value}) -> "
                    f"{self._redact(target)} ({hop_network.value})"
                )

            log.debug("redirect %d: %s", hop + 1, self._redact(target))
            current = target
            if method.upper() not in ("GET", "HEAD") and resp.status in (301, 302, 303):
                method, body = "GET", None

        raise FetchError(f"too many redirects starting at {self._redact(url)}")

    def _request_once(
        self,
        method: str,
        url: str,
        network: Network,
        headers: dict[str, str] | None,
        params: dict[str, str] | None,
        body: str | None,
    ) -> Response:
        profile = self.profile(network)
        client = self._client(network)

        if profile.respect_robots and not self._robots.allowed(url, client):
            raise FetchError(f"robots.txt disallows {url}")

        rotated_once = False

        @retry(
            stop=stop_after_attempt(profile.attempts),
            wait=wait_exponential(multiplier=2, min=2, max=60),
            retry=retry_if_exception_type((httpx.HTTPError, FetchError)),
            reraise=True,
        )
        def _do() -> Response:
            nonlocal rotated_once
            self._throttle(url, network, profile)
            try:
                resp = client.request(
                    method.upper(),
                    url,
                    headers=headers or None,
                    params=params or None,
                    content=body.encode("utf-8") if body else None,
                )
            except httpx.HTTPError as exc:
                if (
                    network is Network.TOR
                    and not rotated_once
                    and self.settings.tor_rotate_on_failure
                    and isinstance(exc, CIRCUIT_ERRORS)
                ):
                    rotated_once = True
                    ok, why = self.tor.new_circuit()
                    log.debug("circuit rotation after %s: %s", type(exc).__name__, why)
                raise FetchError(
                    f"{method} {self._redact(url)} failed: {self._redact(str(exc))}"
                ) from exc

            if resp.status_code in RETRYABLE_STATUS:
                raise FetchError(f"{method} {self._redact(url)} returned {resp.status_code}")
            if resp.status_code >= 400:
                raise FetchError(f"{method} {self._redact(url)} returned {resp.status_code}")

            return Response(
                url=str(resp.url),
                status=resp.status_code,
                text=resp.text,
                content=resp.content,
                headers=dict(resp.headers),
                network=network.value,
            )

        return _do()
