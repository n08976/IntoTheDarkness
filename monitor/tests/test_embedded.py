"""Reading data a JavaScript app embedded in its own HTML — no browser needed."""

from __future__ import annotations

import json

import pytest
from test_scrapers import FakeFetcher

from intothedarkness.enrich import SectorClassifier
from intothedarkness.models import Target
from intothedarkness.scrapers import get_scraper
from intothedarkness.scrapers.embedded import extract_payloads

VICTIMS = [
    {"id": "49", "title": "Italtel Peru", "postCount": 2, "date": "yesterday"},
    {"id": "48", "title": "CCA Bank", "postCount": 2, "date": "Aug 20"},
    {"id": "47", "title": "Mansfield Family Dentistry", "postCount": 2, "date": "Jul 23"},
]


def inertia_page(payload: dict) -> str:
    """The shape Everest actually serves: empty body, JSON in a script tag."""
    return (
        "<html><head><title>Everest</title></head><body>"
        f'<script data-page="app" type="application/json">{json.dumps(payload)}</script>'
        '<div id="app"></div></body></html>'
    )


def next_page(payload: dict) -> str:
    return (
        "<html><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{json.dumps(payload)}</script>'
        "</body></html>"
    )


def nuxt_page(payload: dict) -> str:
    return f"<html><body><script>window.__NUXT__ = {json.dumps(payload)};</script></body></html>"


def target(**kw) -> Target:
    base = dict(
        name="everest",
        url="http://x.onion/",
        scraper="embedded",
        json_path="props.categories",
        json_fields={"key": "id", "title": "title", "posts": "postCount", "published": "date"},
    )
    return Target(**{**base, **kw})


def scrape(html: str, tgt: Target | None = None):
    scraper = get_scraper("embedded", FakeFetcher(html, url="http://x.onion/"))
    scraper.classifier = SectorClassifier()
    return scraper.scrape(tgt or target())


# ------------------------------------------------------------------ extraction


def test_finds_an_inertia_payload():
    html = inertia_page({"component": "Public/News", "props": {"categories": VICTIMS}})
    labels = [label for label, _ in extract_payloads(html)]
    assert any("app" in str(label) or "json" in str(label) for label in labels)


def test_finds_a_next_data_payload():
    payload = {"props": {"pageProps": {"items": VICTIMS}}}
    assert extract_payloads(next_page(payload))[0][1] == payload


def test_finds_a_nuxt_window_assignment():
    payload = {"data": [{"categories": VICTIMS}]}
    found = extract_payloads(nuxt_page(payload))
    assert found and found[0][1] == payload


def test_braces_inside_strings_do_not_truncate_the_payload():
    payload = {"props": {"note": "a } brace and a { brace", "categories": VICTIMS}}
    found = extract_payloads(nuxt_page(payload))
    assert found[0][1]["props"]["note"] == "a } brace and a { brace"
    assert len(found[0][1]["props"]["categories"]) == 3


def test_unparseable_script_is_skipped_not_fatal():
    html = '<html><body><script type="application/json">{not json</script></body></html>'
    assert extract_payloads(html) == []


def test_pages_without_embedded_json_yield_nothing():
    assert extract_payloads("<html><body><p>plain page</p></body></html>") == []


# -------------------------------------------------------------------- scraping


def test_extracts_victims_from_an_empty_bodied_spa():
    """The whole point: a page with no visible text still yields its records."""
    html = inertia_page({"component": "Public/News", "props": {"categories": VICTIMS}})
    items = scrape(html)

    assert [i.title for i in items] == [v["title"] for v in VICTIMS]
    assert items[0].fields["posts"] == 2
    assert items[0].fields["published"] == "yesterday"


def test_sectors_are_labelled_from_the_name():
    html = inertia_page({"component": "x", "props": {"categories": VICTIMS}})
    sectors = {i.title: i.sector for i in scrape(html)}
    assert sectors["CCA Bank"] == "finance"
    assert sectors["Mansfield Family Dentistry"] == "healthcare"


def test_keys_are_stable_across_reordering():
    html_a = inertia_page({"props": {"categories": VICTIMS}})
    html_b = inertia_page({"props": {"categories": list(reversed(VICTIMS))}})
    assert {i.key for i in scrape(html_a)} == {i.key for i in scrape(html_b)}


def test_works_with_next_data_too():
    html = next_page({"props": {"pageProps": {"items": VICTIMS}}})
    tgt = target(json_path="props.pageProps.items")
    assert len(scrape(html, tgt)) == 3


def test_missing_json_path_names_the_payloads_it_saw():
    html = inertia_page({"props": {"categories": VICTIMS}})
    with pytest.raises(ValueError, match="matched nothing"):
        scrape(html, target(json_path="props.nope"))


def test_a_page_with_no_embedded_json_says_a_browser_may_be_needed():
    with pytest.raises(ValueError, match="may genuinely require a browser"):
        scrape("<html><body><p>nothing here</p></body></html>")


def test_non_list_json_path_is_rejected():
    html = inertia_page({"props": {"categories": 5}})
    with pytest.raises(ValueError, match="expected a list"):
        scrape(html)


def test_include_filter_applies():
    html = inertia_page({"props": {"categories": VICTIMS}})
    assert [i.title for i in scrape(html, target(include="bank"))] == ["CCA Bank"]
