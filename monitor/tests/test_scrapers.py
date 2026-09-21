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


def test_json_scraper_rejects_a_rate_limit_envelope():
    # ransomware.live answers a throttled request with {"message": "1 per 1
    # minute"}. Wrapped as a single item it would look like a feed that went
    # quiet, and a quiet feed is exactly what "no new victims" looks like.
    target = Target(name="feed", url="https://api.example.com/x", scraper="json")
    with pytest.raises(ValueError, match="error envelope"):
        get_scraper("json", FakeFetcher(json.dumps({"message": "1 per 1 minute"}))).scrape(target)


def test_json_scraper_still_accepts_a_single_record_object():
    # Narrow sentinel: a dict carrying real fields is a one-record feed, not a
    # complaint, and must keep working.
    payload = json.dumps({"id": 1, "name": "Only"})
    target = Target(
        name="feed", url="https://api.example.com/x", scraper="json",
        json_fields={"key": "id", "title": "name"},
    )
    items = get_scraper("json", FakeFetcher(payload)).scrape(target)
    assert [i.title for i in items] == ["Only"]


RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/">
<channel><title>Wire</title><link>https://wire.example/</link>
<item>
  <title>Mercy Hospital hit by ransomware</title>
  <link>https://wire.example/mercy-hospital</link>
  <guid isPermaLink="false">https://wire.example/?p=41</guid>
  <pubDate>Sun, 20 Sep 2026 13:21:17 +0000</pubDate>
  <category><![CDATA[Breaches]]></category>
  <description><![CDATA[<p>Systems <b>offline</b> since Friday.</p>]]></description>
</item>
<item>
  <title>Quiet week</title>
  <link>/relative-path</link>
  <pubDate>Sat, 19 Sep 2026 08:00:00 +0000</pubDate>
</item>
</channel></rss>"""

ATOM = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Advisories</title>
<entry>
  <title>AA26-263A: Actors exploiting clinic VPNs</title>
  <link rel="alternate" href="https://adv.example/aa26-263a"/>
  <link rel="enclosure" href="https://adv.example/aa26-263a.pdf"/>
  <id>urn:uuid:1</id>
  <updated>2026-09-20T10:00:00Z</updated>
  <summary>Patch now.</summary>
</entry></feed>"""


def test_rss_scraper_reads_links_dates_and_strips_html_from_bodies():
    # Parsed as XML on purpose: an HTML parser treats RSS <link> as a void
    # element and drops the article URL without any error.
    target = Target(name="wire", url="https://wire.example/feed/", scraper="rss")
    items = get_scraper("rss", FakeFetcher(RSS, url="https://wire.example/feed/")).scrape(target)

    assert [i.title for i in items] == ["Mercy Hospital hit by ransomware", "Quiet week"]
    assert items[0].url == "https://wire.example/mercy-hospital"
    assert items[1].url == "https://wire.example/relative-path"      # resolved
    assert items[0].fields["published"] == "Sun, 20 Sep 2026 13:21:17 +0000"
    assert items[0].fields["guid"] == "https://wire.example/?p=41"
    assert items[0].fields["category"] == "Breaches"
    assert items[0].text == "Systems offline since Friday."         # HTML gone
    assert items[0].key != items[1].key


def test_rss_scraper_keys_on_guid_so_a_retitled_post_is_not_a_new_one():
    target = Target(name="wire", url="https://wire.example/feed/", scraper="rss")
    first = get_scraper("rss", FakeFetcher(RSS)).scrape(target)[0]
    retitled = RSS.replace("Mercy Hospital hit by ransomware", "UPDATED: Mercy Hospital")
    second = get_scraper("rss", FakeFetcher(retitled)).scrape(target)[0]
    assert first.key == second.key


def test_rss_scraper_handles_atom_and_prefers_the_alternate_link():
    target = Target(name="adv", url="https://adv.example/feed", scraper="rss")
    items = get_scraper("rss", FakeFetcher(ATOM)).scrape(target)
    assert len(items) == 1
    assert items[0].url == "https://adv.example/aa26-263a"          # not the pdf
    assert items[0].fields["published"] == "2026-09-20T10:00:00Z"
    assert items[0].text == "Patch now."


def test_rss_scraper_rejects_a_body_that_is_not_a_feed():
    # A Cloudflare challenge page is HTML with no feed root; it must be an
    # error, not an empty feed that reads as "nothing new".
    target = Target(name="wire", url="https://wire.example/feed/", scraper="rss")
    with pytest.raises(ValueError, match="not an RSS or Atom feed"):
        challenge = "<html><title>Just a moment...</title></html>"
        get_scraper("rss", FakeFetcher(challenge)).scrape(target)


DARKFIELD = """<?xml version="1.0"?><rss version="2.0"><channel><title>Darkfield</title>
<item><title>Hudson MD Group, LLC — claimed by metaencryptor</title>
  <link>https://darkfield.orizon.one/victims/cf49d648</link>
  <pubDate>Mon, 21 Sep 2026 13:08:14 GMT</pubDate>
  <description>Healthcare · US · data_published</description></item>
<item><title>Weekly roundup</title>
  <link>https://darkfield.orizon.one/pulse/1</link>
  <pubDate>Mon, 21 Sep 2026 09:00:00 GMT</pubDate>
  <description>Editorial</description></item>
</channel></rss>"""


def test_darkfield_unpacks_victim_operator_sector_and_country():
    # Darkfield's feed writes the facts as prose; the pipeline wants the
    # victim as the title and the rest as fields, with the sector counted as
    # an upstream label rather than a guess from the name.
    target = Target(name="df", url="https://darkfield.orizon.one/feed.xml", scraper="darkfield")
    items = get_scraper("darkfield", FakeFetcher(DARKFIELD)).scrape(target)

    assert items[0].title == "Hudson MD Group, LLC"
    assert items[0].fields["group"] == "metaencryptor"
    assert items[0].fields["sector"] == "Healthcare"
    assert items[0].fields["country"] == "US"
    assert items[0].fields["status"] == "data_published"
    assert items[0].fields["published"] == "Mon, 21 Sep 2026 13:08:14 GMT"
    assert items[0].url.endswith("/victims/cf49d648")

    # An entry that is not a claim passes through untouched.
    assert items[1].title == "Weekly roundup"
    assert "group" not in items[1].fields
    assert items[1].fields["sector"] == "Editorial"
