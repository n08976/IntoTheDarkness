"""Onion address handling, redaction, and the Tor transport path."""

from __future__ import annotations

import pytest
from socks_stub import SocksStub, http_response

from intothedarkness.scrapers.fetch import (
    Fetcher,
    FetchError,
    Network,
    TorNotConfigured,
    resolve_network,
)
from intothedarkness.tor import check_socks, is_onion, redact, validate_onion

V3 = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"
V2 = "expyuzz4wqqyqhjn.onion"


# ------------------------------------------------------------------- addresses


def test_v3_address_accepted():
    result = validate_onion(f"http://{V3}/path")
    assert result.ok and result.version == 3


def test_v2_address_rejected_with_a_reason():
    result = validate_onion(f"http://{V2}/")
    assert not result.ok and result.version == 2
    assert "deprecated" in result.reason


def test_malformed_address_reports_the_length():
    result = validate_onion("http://tooshort.onion/")
    assert not result.ok and "expected 56" in result.reason


def test_clearnet_is_not_an_onion():
    assert not is_onion("https://example.com")
    assert is_onion(f"http://{V3}/")
    assert is_onion(f"HTTP://{V3.upper()}/")


def test_redaction_masks_the_middle_but_stays_recognisable():
    masked = redact(f"GET http://{V3}/x failed")
    assert V3 not in masked
    assert masked.startswith("GET http://duckdu")
    assert ".onion" in masked


def test_redaction_can_be_disabled():
    assert V3 in redact(f"http://{V3}/", enabled=False)


# --------------------------------------------------------------------- routing


def test_onion_routes_to_tor_and_clearnet_stays_direct():
    assert resolve_network(f"http://{V3}/") is Network.TOR
    assert resolve_network("https://example.com") is Network.DIRECT


def test_explicit_override_wins_over_the_url():
    assert resolve_network("https://example.com", Network.TOR) is Network.TOR
    assert resolve_network(f"http://{V3}/", Network.DIRECT) is Network.DIRECT


def test_malformed_onion_fails_before_any_connection(settings):
    settings.tor_socks_url = "socks5://127.0.0.1:1"  # would refuse instantly
    with Fetcher(settings) as f, pytest.raises(FetchError, match="expected 56"):
        f.get("http://tooshort.onion/")


def test_tor_disabled_is_a_clear_error(settings):
    settings.tor_enabled = False
    with Fetcher(settings) as f, pytest.raises(TorNotConfigured, match="tor_enabled"):
        f.get(f"http://{V3}/")


# ------------------------------------------------------------------- transport


def test_fetch_over_socks_sends_the_hostname_not_an_ip(settings):
    """The proxy must receive the .onion name, or Tor cannot resolve it."""
    with SocksStub(response=http_response("<html><body>hidden</body></html>")) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as f:
            resp = f.get(f"http://{V3}/page")

        assert resp.status == 200
        assert "hidden" in resp.text
        assert resp.network == "tor"
        # ATYP 0x03 is DOMAIN_NAME: the name was passed through for remote
        # resolution rather than being looked up locally.
        assert proxy.atypes == [0x03]
        assert proxy.requested == [(V3, 80)]


def test_clearnet_over_tor_when_explicitly_requested(settings):
    with SocksStub(response=http_response("<html>via tor</html>")) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as f:
            resp = f.request("GET", "http://example.com/x", network=Network.TOR)

        assert "via tor" in resp.text
        assert proxy.requested == [("example.com", 80)]


def test_socks_failure_surfaces_as_a_fetch_error(settings):
    # 0x05 is "connection refused by destination host".
    with SocksStub(fail_code=0x05) as proxy:
        settings.tor_socks_url = proxy.url
        settings.tor_max_retries = 1
        with Fetcher(settings) as f, pytest.raises(FetchError) as excinfo:
            f.get(f"http://{V3}/")

    # The onion address must not appear unredacted in the error text.
    assert V3 not in str(excinfo.value)


def test_direct_and_tor_use_separate_clients(settings):
    with SocksStub(response=http_response("ok")) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as f:
            tor_client = f._client(Network.TOR)
            direct_client = f._client(Network.DIRECT)
            assert tor_client is not direct_client
            # Reused within a sweep rather than rebuilt per request.
            assert f._client(Network.TOR) is tor_client


def test_tor_profile_differs_from_direct(settings):
    with Fetcher(settings) as f:
        tor = f.profile(Network.TOR)
        direct = f.profile(Network.DIRECT)

    assert tor.timeout > direct.timeout        # Tor is slower
    assert tor.attempts <= direct.attempts     # and retrying costs more
    assert tor.respect_robots is False         # robots is noise on hidden services
    assert tor.verify is False                 # onion v3 is self-authenticating


def test_check_socks_reports_a_missing_daemon(settings):
    settings.tor_socks_url = "socks5://127.0.0.1:1"
    ok, why = check_socks(settings, timeout=1.0)
    assert not ok and "no SOCKS proxy" in why


def test_check_socks_sees_a_live_proxy(settings):
    with SocksStub() as proxy:
        settings.tor_socks_url = proxy.url
        ok, why = check_socks(settings, timeout=2.0)
    assert ok and "reachable" in why


# ------------------------------------------------------------ redirect boundary


def test_redirect_from_onion_to_clearnet_is_refused(settings):
    """A hidden service must not be able to bounce us onto the open internet."""
    redirect = (
        b"HTTP/1.1 302 Found\r\nLocation: https://example.com/tracked\r\n"
        b"Content-Length: 0\r\nConnection: close\r\n\r\n"
    )
    with SocksStub(response=redirect) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as f, pytest.raises(FetchError) as excinfo:
            f.get(f"http://{V3}/go")

    message = str(excinfo.value)
    assert "refusing redirect across networks" in message
    assert "example.com" in message
    assert V3 not in message  # still redacted


def test_redirect_within_tor_is_followed(settings):
    other = "a" * 56 + ".onion"
    with SocksStub() as proxy:
        settings.tor_socks_url = proxy.url
        proxy.response = (
            f"HTTP/1.1 302 Found\r\nLocation: http://{other}/next\r\n"
            "Content-Length: 0\r\nConnection: close\r\n\r\n"
        ).encode()
        with Fetcher(settings) as f, pytest.raises(FetchError, match="too many redirects"):
            f.get(f"http://{V3}/go")

        # Every hop stayed inside Tor rather than escaping to clearnet.
        assert {host for host, _ in proxy.requested} <= {V3, other}
        assert len(proxy.requested) > 1


# ----------------------------------------------------------- socks diagnostics


def test_probe_reports_no_listener_on_a_closed_port():
    from intothedarkness.tor import probe_socks

    probe = probe_socks(port=1, timeout=2.0)
    assert not probe.listening and not probe.usable and not probe.bootstrapping
    assert "no listener" in probe.detail


def test_probe_reports_a_usable_proxy():
    from intothedarkness.tor import probe_socks

    with SocksStub() as proxy:
        probe = probe_socks(host=proxy.host, port=proxy.port, timeout=5.0)
    assert probe.listening and probe.usable
    assert probe.reply_code == 0x00
    assert not probe.bootstrapping


def test_probe_distinguishes_bootstrapping_from_working():
    """A tor still bootstrapping accepts the connection then refuses to route.

    A bare TCP connect cannot see the difference, which is the whole reason for
    doing a full SOCKS5 CONNECT.
    """
    from intothedarkness.tor import probe_socks

    with SocksStub(fail_code=0x01) as proxy:  # general SOCKS server failure
        probe = probe_socks(host=proxy.host, port=proxy.port, timeout=5.0)

    assert probe.listening
    assert not probe.usable
    assert probe.bootstrapping
    assert probe.reply_code == 0x01
    assert "general SOCKS server failure" in probe.detail


def test_probe_asks_for_the_target_by_hostname():
    """The probe itself must not leak a DNS lookup either."""
    from intothedarkness.tor import probe_socks

    with SocksStub() as proxy:
        probe_socks(host=proxy.host, port=proxy.port, through="check.torproject.org")
        assert proxy.atypes == [0x03]
        assert proxy.requested == [("check.torproject.org", 443)]


def test_find_socks_skips_ports_with_nothing_on_them():
    from intothedarkness.tor import find_socks

    # Nothing is listening on the conventional ports in a test environment.
    assert all(probe.listening for _, _, probe in find_socks(timeout=1.0))
