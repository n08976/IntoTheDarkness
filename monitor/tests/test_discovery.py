"""Discovering candidate sites through onion search engines."""

from __future__ import annotations

from socks_stub import SocksStub, http_response

from intothedarkness.bookmarks import Bookmarks, Category, Link
from intothedarkness.discovery import (
    ContentFilter,
    Engine,
    adaptive_threshold,
    default_engines,
    load_engines,
    parse_results,
    search,
)
from intothedarkness.scrapers import Fetcher

AHMIA = "juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion"
V1 = "a" * 56 + ".onion"
V2 = "b" * 56 + ".onion"
V3 = "c" * 56 + ".onion"


def engine(name="ahmia", selector="li.result a", host=AHMIA) -> Engine:
    return Engine(name=name, url=f"http://{host}/search/?q={{query}}", result_selector=selector)


def results_page(entries: list[tuple[str, str]], host: str = AHMIA) -> str:
    rows = "".join(
        f'<li class="result"><a href="{href}">{title}</a></li>' for href, title in entries
    )
    return (
        f'<html><body><nav><a href="http://{host}/">Home</a></nav>'
        f"<ol>{rows}</ol></body></html>"
    )


# ---------------------------------------------------------------------- engines


def test_default_catalogue_is_well_formed():
    engines = default_engines()
    assert len(engines) >= 10
    assert all(e.validate() is None for e in engines)
    assert all("{query}" in e.url for e in engines)


def test_query_url_encodes_the_query():
    assert "acme+corp" in engine().query_url("acme corp")


def test_engine_without_query_placeholder_is_rejected():
    assert "placeholder" in Engine(name="x", url="http://x.onion/").validate()


def test_engines_load_from_yaml(tmp_path):
    path = tmp_path / "engines.yaml"
    path.write_text(
        "engines:\n"
        f"  - name: mine\n    url: http://{V1}/?q={{query}}\n    result_selector: .r a\n"
    )
    engines = load_engines(path)
    assert [e.name for e in engines] == ["mine"]
    assert engines[0].result_selector == ".r a"


def test_missing_or_empty_yaml_falls_back_to_defaults(tmp_path):
    assert len(load_engines(tmp_path / "absent.yaml")) >= 10
    empty = tmp_path / "empty.yaml"
    empty.write_text("engines: []\n")
    assert len(load_engines(empty)) >= 10


# ---------------------------------------------------------------------- parsing


def test_extracts_onions_from_hrefs():
    page = results_page(
        [(f"http://{V1}/blog", "Qilin Leak Blog"), (f"http://{V2}/", "Other Site")]
    )
    hits = parse_results(page, engine(), f"http://{AHMIA}/search/")
    assert {h.host for h in hits} == {V1, V2}


def test_unwraps_redirect_wrappers():
    page = results_page(
        [(f"/search/redirect?redirect_url=http%3A%2F%2F{V1}%2Fx", "Wrapped Result")]
    )
    hits = parse_results(page, engine(), f"http://{AHMIA}/search/")
    assert [h.host for h in hits] == [V1]


def test_ignores_body_text_mentions():
    """Only hrefs count; an address mentioned in prose is not a search result."""
    page = (
        '<html><body><ol><li class="result">'
        f'<a href="/x">see {V1} for more</a></li></ol></body></html>'
    )
    assert parse_results(page, engine(), f"http://{AHMIA}/search/") == []


def test_drops_engine_self_links():
    page = results_page([(f"http://{AHMIA}/search/?q=y", "Search again")])
    assert parse_results(page, engine(), f"http://{AHMIA}/search/") == []


def test_drops_v2_and_malformed_addresses():
    page = results_page([("http://tooshort.onion/", "Old v2 Site")])
    assert parse_results(page, engine(), f"http://{AHMIA}/search/") == []


def test_drops_navigation_titles_and_stubs():
    page = results_page([
        (f"http://{V1}/", "Home"),
        (f"http://{V2}/", "a"),
        (f"http://{V3}/", "Genuine Result Title"),
    ])
    assert [h.host for h in parse_results(page, engine(), f"http://{AHMIA}/search/")] == [V3]


def test_selector_miss_yields_nothing_rather_than_every_link():
    """An unrecognised layout must produce no candidates, not a page of nav junk."""
    page = results_page([(f"http://{V1}/", "Real Result")])
    strict = engine(selector=".does-not-exist")
    assert parse_results(page, strict, f"http://{AHMIA}/search/") == []


def test_engine_without_a_selector_may_scan_all_links():
    page = results_page([(f"http://{V1}/", "Real Result")])
    loose = engine(selector=None)
    assert [h.host for h in parse_results(page, loose, f"http://{AHMIA}/search/")] == [V1]


# ------------------------------------------------------------------- ranking


def test_adaptive_threshold_rises_with_responding_engines():
    assert adaptive_threshold(1) == 1
    assert adaptive_threshold(2) == 1
    assert adaptive_threshold(3) == 2
    assert adaptive_threshold(9) == 2
    assert adaptive_threshold(9, requested=1) == 1  # explicit override wins


# ------------------------------------------------------------ end to end search


def test_search_through_tor_collapses_and_ranks(settings):
    """The full path: SOCKS5, engine pages, parsing, corroboration, dedupe."""
    book = Bookmarks(
        categories=[Category(name="Known", links=[Link("Existing", f"http://{V3}/")])]
    )
    page = results_page([
        (f"http://{V1}/", "Widely Listed Site"),
        (f"http://{V2}/", "Seen Once Only"),
        (f"http://{V3}/", "Already In The List"),
    ])

    with SocksStub(response=http_response(page)) as proxy:
        settings.tor_socks_url = proxy.url
        engines = [engine(name=f"e{i}") for i in range(3)]
        with Fetcher(settings) as fetcher:
            report = search("acme", engines, fetcher, known_hosts=book.hosts())

    assert report.responded == 3
    assert report.threshold == 2                    # three engines answered
    hosts = [c.host for c in report.candidates]
    assert V3 not in hosts                          # already curated
    assert V1 in hosts and V2 in hosts              # both hit all three engines
    assert report.candidates[0].corroboration == 3


def test_search_skips_addresses_already_curated(settings):
    page = results_page([(f"http://{V1}/", "Already Known Site")])
    with SocksStub(response=http_response(page)) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as fetcher:
            report = search("q", [engine()], fetcher, known_hosts={V1})
    assert report.candidates == []
    assert report.hits == 1                         # found, then deliberately dropped


def test_search_records_engine_failures_without_aborting(settings):
    page = results_page([(f"http://{V1}/", "Good Result")])
    with SocksStub(response=http_response(page)) as proxy:
        settings.tor_socks_url = proxy.url
        settings.tor_max_retries = 1
        good = engine(name="good")
        broken = Engine(name="broken", url="http://x.onion/?q={query}")  # invalid v3 host
        with Fetcher(settings) as fetcher:
            report = search("q", [good, broken], fetcher, min_engines=1)

    assert "broken" in report.errors
    assert [c.host for c in report.candidates] == [V1]


def test_clearnet_engines_are_routed_over_tor_by_default(settings):
    """A clearnet index must not put the query on the local network's DNS."""
    page = results_page([(f"http://{V1}/", "Result From Clearnet Index")], host="ahmia.fi")
    with SocksStub(response=http_response(page)) as proxy:
        settings.tor_socks_url = proxy.url
        clearnet = Engine(name="ahmia-clearnet", url="https://ahmia.fi/search/?q={query}",
                          result_selector="li.result a")
        with Fetcher(settings) as fetcher:
            search("q", [clearnet], fetcher, min_engines=1)

    # It went through the SOCKS proxy rather than direct.
    assert proxy.requested and proxy.requested[0][0] == "ahmia.fi"


# -------------------------------------------------------------------- safety


def test_content_filter_withholds_and_counts_without_printing(settings):
    page = results_page([
        (f"http://{V1}/", "Legitimate Leak Site"),
        (f"http://{V2}/", "csam directory listing"),
    ])
    with SocksStub(response=http_response(page)) as proxy:
        settings.tor_socks_url = proxy.url
        with Fetcher(settings) as fetcher:
            report = search("q", [engine()], fetcher, min_engines=1)

    assert [c.host for c in report.candidates] == [V1]
    assert report.withheld == 1                     # counted, so nothing is silent
    assert "withheld by the content filter" in report.summary()


def test_content_filter_names_the_term_not_the_content():
    verdict = ContentFilter().check("some csam listing here")
    assert verdict.blocked
    assert "csam" in verdict.reason
    assert "listing here" not in verdict.reason


def test_content_filter_allows_ordinary_threat_intel_language():
    allowed = ContentFilter()
    assert allowed.allows("Qilin ransomware leak site")
    assert allowed.allows("Acme Corp breach data 42GB")


def test_content_filter_accepts_extra_terms():
    assert not ContentFilter(extra_terms=["banned-phrase"]).allows("a banned-phrase here")
