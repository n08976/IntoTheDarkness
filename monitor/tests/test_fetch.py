"""Fetcher behaviour: retries, robots.txt, throttling, error mapping."""

from __future__ import annotations

import httpx
import pytest
import respx

from intothedarkness.scrapers.fetch import Fetcher, FetchError


@pytest.fixture
def fetcher(settings):
    with Fetcher(settings) as f:
        yield f


@respx.mock
def test_get_returns_body_and_final_url(fetcher):
    respx.get("https://e.com/p").mock(return_value=httpx.Response(200, text="hello"))
    resp = fetcher.get("https://e.com/p")
    assert resp.status == 200
    assert resp.text == "hello"


@respx.mock
def test_redirects_are_followed_and_final_url_reported(fetcher):
    respx.get("https://e.com/old").mock(
        return_value=httpx.Response(302, headers={"Location": "https://e.com/new"})
    )
    respx.get("https://e.com/new").mock(return_value=httpx.Response(200, text="moved"))
    resp = fetcher.get("https://e.com/old")
    assert resp.url == "https://e.com/new"
    assert resp.text == "moved"


@respx.mock
def test_retryable_status_is_retried_then_succeeds(settings):
    route = respx.get("https://e.com/flaky").mock(
        side_effect=[
            httpx.Response(503),
            httpx.Response(503),
            httpx.Response(200, text="finally"),
        ]
    )
    with Fetcher(settings) as f:  # settings fixture allows 3 attempts
        assert f.get("https://e.com/flaky").text == "finally"
    assert route.call_count == 3


@respx.mock
def test_retries_are_bounded_and_raise_fetch_error(settings):
    route = respx.get("https://e.com/down").mock(return_value=httpx.Response(503))
    with Fetcher(settings) as f, pytest.raises(FetchError, match="503"):
        f.get("https://e.com/down")
    assert route.call_count == settings.max_retries


@respx.mock
def test_client_errors_are_not_retried_more_than_configured(settings):
    route = respx.get("https://e.com/gone").mock(return_value=httpx.Response(404))
    with Fetcher(settings) as f, pytest.raises(FetchError, match="404"):
        f.get("https://e.com/gone")
    assert route.call_count <= settings.max_retries


@respx.mock
def test_transport_errors_become_fetch_errors(settings):
    respx.get("https://e.com/x").mock(side_effect=httpx.ConnectError("no route"))
    with Fetcher(settings) as f, pytest.raises(FetchError, match="failed"):
        f.get("https://e.com/x")


@respx.mock
def test_user_agent_and_custom_headers_are_sent(fetcher, settings):
    route = respx.get("https://e.com/h").mock(return_value=httpx.Response(200, text=""))
    fetcher.request("GET", "https://e.com/h", headers={"X-Trace": "1"})
    sent = route.calls[0].request.headers
    assert sent["user-agent"] == settings.user_agent
    assert sent["x-trace"] == "1"


@respx.mock
def test_robots_disallow_blocks_the_fetch(settings):
    settings.respect_robots = True
    respx.get("https://e.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nDisallow: /private")
    )
    blocked = respx.get("https://e.com/private/x").mock(
        return_value=httpx.Response(200, text="secret")
    )
    allowed = respx.get("https://e.com/public").mock(
        return_value=httpx.Response(200, text="fine")
    )

    with Fetcher(settings) as f:
        with pytest.raises(FetchError, match="robots.txt disallows"):
            f.get("https://e.com/private/x")
        assert f.get("https://e.com/public").text == "fine"

    assert blocked.call_count == 0
    assert allowed.call_count == 1


@respx.mock
def test_robots_is_fetched_once_per_host(settings):
    settings.respect_robots = True
    robots = respx.get("https://e.com/robots.txt").mock(
        return_value=httpx.Response(200, text="User-agent: *\nAllow: /")
    )
    respx.get("https://e.com/a").mock(return_value=httpx.Response(200, text=""))
    respx.get("https://e.com/b").mock(return_value=httpx.Response(200, text=""))

    with Fetcher(settings) as f:
        f.get("https://e.com/a")
        f.get("https://e.com/b")

    assert robots.call_count == 1


@respx.mock
def test_missing_robots_defaults_to_allowed(settings):
    settings.respect_robots = True
    respx.get("https://e.com/robots.txt").mock(return_value=httpx.Response(404))
    respx.get("https://e.com/x").mock(return_value=httpx.Response(200, text="ok"))
    with Fetcher(settings) as f:
        assert f.get("https://e.com/x").text == "ok"


@respx.mock
def test_unreachable_robots_does_not_block_the_fetch(settings):
    settings.respect_robots = True
    respx.get("https://e.com/robots.txt").mock(side_effect=httpx.ConnectError("nope"))
    respx.get("https://e.com/x").mock(return_value=httpx.Response(200, text="ok"))
    with Fetcher(settings) as f:
        assert f.get("https://e.com/x").text == "ok"


@respx.mock
def test_per_host_delay_throttles_successive_requests(settings):
    import time

    settings.per_host_delay = 0.25
    respx.get("https://e.com/a").mock(return_value=httpx.Response(200, text=""))
    respx.get("https://e.com/b").mock(return_value=httpx.Response(200, text=""))

    with Fetcher(settings) as f:
        start = time.monotonic()
        f.get("https://e.com/a")
        f.get("https://e.com/b")
        elapsed = time.monotonic() - start

    assert elapsed >= 0.25


@respx.mock
def test_post_body_and_params_are_forwarded(fetcher):
    route = respx.post("https://e.com/api").mock(return_value=httpx.Response(200, text="{}"))
    fetcher.request("POST", "https://e.com/api", params={"q": "x"}, body='{"a":1}')
    request = route.calls[0].request
    assert request.url.params["q"] == "x"
    assert request.content == b'{"a":1}'


# --- access-queue / anti-DDoS gate (CL0P-style leak sites) -------------------

_GATE = (
    "<html><head><title>CL0P^_- LEAKS Access Queue</title></head>"
    "<body>You have been placed in a queue, awaiting forwarding.</body></html>"
)
_REAL = "<html><head><title>HOME | CL0P^_- LEAKS</title></head><body>victims</body></html>"
_BAN = "403 DDOS filter killed your path. (You probably sent too many requests at once)."


@respx.mock
def test_wait_for_queue_sits_through_the_gate_then_returns_content(settings, monkeypatch):
    monkeypatch.setattr("intothedarkness.scrapers.fetch.time.sleep", lambda *_: None)
    route = respx.get("https://e.com/").mock(
        side_effect=[
            httpx.Response(200, text=_GATE, headers={"Refresh": "5"}),
            httpx.Response(200, text=_GATE, headers={"Refresh": "1"}),
            httpx.Response(200, text=_REAL),
        ]
    )
    with Fetcher(settings) as f:
        resp = f.get("https://e.com/", wait_for_queue=True)
    assert "HOME" in resp.text
    assert route.call_count == 3


@respx.mock
def test_queue_gate_is_returned_as_is_without_the_flag(settings):
    route = respx.get("https://e.com/").mock(
        return_value=httpx.Response(200, text=_GATE, headers={"Refresh": "5"})
    )
    with Fetcher(settings) as f:
        resp = f.get("https://e.com/")
    assert "Access Queue" in resp.text  # no waiting, no extra requests
    assert route.call_count == 1


@respx.mock
def test_queue_ban_rotates_the_circuit_then_recovers(settings, monkeypatch):
    # The ban remedy ("grab a new identity") only makes sense over Tor, so this
    # one runs on an onion where _reset_gate asks for a fresh circuit.
    onion = "http://clopofffr7tth6hsvxx3cys4qkshrbbiitnxfwts6hj64mtnaovjieyd.onion/"
    monkeypatch.setattr("intothedarkness.scrapers.fetch.time.sleep", lambda *_: None)
    rotations: list[int] = []
    respx.get(onion).mock(
        side_effect=[
            httpx.Response(200, text=_GATE, headers={"Refresh": "2"}),
            httpx.Response(200, text=_BAN),
            httpx.Response(200, text=_REAL),
        ]
    )
    with Fetcher(settings) as f:
        monkeypatch.setattr(f.tor, "new_circuit", lambda: (rotations.append(1), (True, "ok"))[1])
        resp = f.get(onion, wait_for_queue=True)
    assert "HOME" in resp.text
    assert rotations == [1]  # the ban, and only the ban, asked for a new circuit


@respx.mock
def test_queue_that_never_clears_fails_within_the_budget(settings, monkeypatch):
    monkeypatch.setattr("intothedarkness.scrapers.fetch.time.sleep", lambda *_: None)
    route = respx.get("https://e.com/").mock(
        return_value=httpx.Response(200, text=_GATE, headers={"Refresh": "1"})
    )
    with Fetcher(settings) as f, pytest.raises(FetchError, match="access queue"):
        f.get("https://e.com/", wait_for_queue=True)
    # bounded: the first request plus queue_wait_rounds retries, no more
    assert route.call_count == 1 + settings.queue_wait_rounds
