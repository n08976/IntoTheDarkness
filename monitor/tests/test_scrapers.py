from __future__ import annotations

import json

import pytest

from intothedarkness.models import Selectors, Target
from intothedarkness.scrapers import get_scraper
from intothedarkness.scrapers.fetch import Response
from intothedarkness.scrapers.json_api import dig

LISTING = """
<html><head><title>Listing</title></head><body>
  <ul id="results">
    <li class="row"><a href="/a">Alpha breach report</a><p class="desc">First</p></li>
    <li class="row"><a href="/b">Beta newsletter signup</a><p class="desc">Second</p></li>
    <li class="row"><a href="https://other.example/c">Gamma</a><p class="desc">Third</p></li>
  </ul>
</body></html>
"""


class FakeFetcher:
    """Stands in for Fetcher; returns canned bodies instead of doing network IO."""

    def __init__(self, text: str, url: str = "https://example.com/list") -> None:
        self.text = text
        self.url = url
        self.calls: list[tuple] = []

    def request(
        self, method, url, headers=None, params=None, body=None, network="auto", **kw
    ) -> Response:
        self.calls.append((method, url, headers, params, body, network))
        return Response(
            url=self.url,
            status=200,
            text=self.text,
            content=self.text.encode(),
            headers={},
            network=str(network),
        )

    def get(self, url, **kw) -> Response:
        return self.request("GET", url, **kw)


def css_target(**kw) -> Target:
    base = dict(
        name="listing",
        url="https://example.com/list",
        scraper="css",
        selectors=Selectors(item="li.row", title="a", link="a", text="p.desc"),
    )
    return Target(**{**base, **kw})


def test_css_extracts_items_and_resolves_relative_links():
    scraper = get_scraper("css", FakeFetcher(LISTING))
    items = scraper.scrape(css_target())

    assert [i.title for i in items] == [
        "Alpha breach report",
        "Beta newsletter signup",
        "Gamma",
    ]
    assert items[0].url == "https://example.com/a"          # relative, resolved
    assert items[2].url == "https://other.example/c"        # absolute, untouched
    assert items[0].text == "First"


def test_css_keys_are_stable_across_reordering():
    first = get_scraper("css", FakeFetcher(LISTING)).scrape(css_target())

    reordered = LISTING.replace(
        '<li class="row"><a href="/a">Alpha breach report</a><p class="desc">First</p></li>\n    ',
        "",
    ).replace(
        '<ul id="results">',
        '<ul id="results">\n    <li class="row"><a href="/a">Alpha breach report</a>'
        '<p class="desc">First</p></li>',
    )
    second = get_scraper("css", FakeFetcher(reordered)).scrape(css_target())

    assert {i.key for i in first} == {i.key for i in second}


def test_css_include_and_exclude_filters():
    scraper = get_scraper("css", FakeFetcher(LISTING))
    assert len(scraper.scrape(css_target(include="breach"))) == 1
    assert len(scraper.scrape(css_target(exclude="newsletter"))) == 2


def test_css_attrs_support_selector_at_attribute():
    target = css_target(
        selectors=Selectors(item="li.row", title="a", attrs={"href": "a@href"})
    )
    items = get_scraper("css", FakeFetcher(LISTING)).scrape(target)
    assert items[0].fields["href"] == "/a"


def test_css_without_item_selector_is_a_clear_error():
    target = css_target(selectors=Selectors(title="a"))
    with pytest.raises(ValueError, match="selectors.item"):
        get_scraper("css", FakeFetcher(LISTING)).scrape(target)


def test_page_scraper_yields_one_item_with_a_fixed_key():
    target = Target(name="page", url="https://example.com/", scraper="page")
    scraper = get_scraper("page", FakeFetcher(LISTING))
    items = scraper.scrape(target)

    assert len(items) == 1
    assert items[0].title == "Listing"
    assert "Alpha breach report" in items[0].text
    # Key must not depend on content, so edits register as CHANGED.
    edited = get_scraper("page", FakeFetcher(LISTING.replace("Alpha", "Delta"))).scrape(target)
    assert edited[0].key == items[0].key
    assert edited[0].content_hash() != items[0].content_hash()


def test_page_scraper_errors_when_region_selector_misses():
    target = Target(
        name="page", url="https://e.com/", scraper="page", selectors=Selectors(text="#nope")
    )
    with pytest.raises(ValueError, match="matched nothing"):
        get_scraper("page", FakeFetcher(LISTING)).scrape(target)


def test_dig_walks_dicts_and_list_indexes():
    data = {"a": {"b": [{"c": 1}, {"c": 2}]}}
    assert dig(data, "a.b.1.c") == 2
    assert dig(data, "a.missing") is None
    assert dig(data, "a.b.9") is None
    assert dig(data, None) is data


def test_json_scraper_maps_fields():
    payload = json.dumps(
        {"data": {"incidents": [
            {"id": 7, "name": "Outage", "html_url": "https://e.com/7",
             "attributes": {"status": "open"}},
        ]}}
    )
    target = Target(
        name="feed",
        url="https://api.example.com/x",
        scraper="json",
        json_path="data.incidents",
        json_fields={"key": "id", "title": "name", "url": "html_url",
                     "status": "attributes.status"},
    )
    items = get_scraper("json", FakeFetcher(payload)).scrape(target)

    assert len(items) == 1
    assert items[0].title == "Outage"
    assert items[0].url == "https://e.com/7"
    assert items[0].fields == {"status": "open"}


def test_json_scraper_rejects_a_path_that_is_not_a_list():
    target = Target(
        name="feed", url="https://api.example.com/x", scraper="json", json_path="data.count"
    )
    with pytest.raises(ValueError, match="expected a list"):
        get_scraper("json", FakeFetcher(json.dumps({"data": {"count": 3}}))).scrape(target)


def test_unknown_scraper_names_the_registered_ones():
    with pytest.raises(KeyError, match="registered"):
        get_scraper("nope", FakeFetcher(""))


def test_css_attrs_join_multi_valued_attributes():
    """class/rel come back as a list from BeautifulSoup, not a string."""
    html = '<ul><li class="row"><a href="/a" class="one two">Alpha</a></li></ul>'
    target = css_target(
        selectors=Selectors(item="li.row", title="a", attrs={"cls": "a@class"})
    )
    items = get_scraper("css", FakeFetcher(html)).scrape(target)
    assert items[0].fields["cls"] == "one two"


def test_css_missing_attribute_yields_empty_string():
    html = '<ul><li class="row"><a href="/a">Alpha</a></li></ul>'
    target = css_target(
        selectors=Selectors(item="li.row", title="a", attrs={"nope": "a@data-x"})
    )
    items = get_scraper("css", FakeFetcher(html)).scrape(target)
    assert items[0].fields["nope"] == ""
