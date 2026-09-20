"""RSS 2.0 and Atom feeds.

Feeds are how most sources that are not leak sites publish: news, CISA
advisories, breach-notification pages. They are also usually the one door a
site leaves open when everything else sits behind a JavaScript challenge.

Parsed as XML, deliberately. Run through an HTML parser an RSS ``<link>`` is a
void element and its text -- the article URL -- is silently dropped, which is
the kind of failure that produces a feed full of items with no links and no
error.
"""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Item, Target, stable_hash
from .base import Scraper, register

ACCEPT = "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.8"

# Element names that carry each field, RSS first, then Atom, then Dublin Core.
_TITLE = ("title",)
_ID = ("guid", "id")
_DATE = ("pubDate", "published", "updated", "dc:date", "date")
_BODY = ("description", "summary", "content:encoded", "content")


def _text(entry: Tag, names: tuple[str, ...]) -> str:
    for name in names:
        node = entry.find(name)
        if isinstance(node, Tag):
            value = " ".join(node.get_text(" ", strip=True).split())
            if value:
                return value
    return ""


def _plain(value: str) -> str:
    """Feed bodies are usually escaped HTML; a report wants the words."""
    if "<" not in value:
        return value
    return " ".join(BeautifulSoup(value, "lxml").get_text(" ", strip=True).split())


def _link(entry: Tag, base_url: str) -> str:
    # Atom: <link rel="alternate" href="..."/>; RSS: <link>text</link>.
    for node in entry.find_all("link"):
        if not isinstance(node, Tag):
            continue
        href = node.get("href")
        if isinstance(href, list):
            href = href[0] if href else None
        if href and node.get("rel") in (None, "alternate", ["alternate"]):
            return urljoin(base_url, str(href))
        text = node.get_text(strip=True)
        if text:
            return urljoin(base_url, text)
    return ""


@register
class RssScraper(Scraper):
    """One item per feed entry, keyed on the feed's own identifier."""

    name = "rss"

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            {"Accept": ACCEPT, **target.headers},
            target.params,
            target.body,
            network=target.network,
        )
        soup = BeautifulSoup(resp.text, "xml")
        entries = [e for e in soup.find_all(["item", "entry"]) if isinstance(e, Tag)]
        if not entries and soup.find(["rss", "feed", "RDF"]) is None:
            raise ValueError(
                f"target {target.name!r}: {target.url} is not an RSS or Atom feed "
                f"(HTTP {resp.status}, {len(resp.text)} bytes)"
            )

        items: list[Item] = []
        for entry in entries:
            title = _text(entry, _TITLE)
            link = _link(entry, resp.url)
            guid = _text(entry, _ID)
            published = _text(entry, _DATE)
            body = _plain(_text(entry, _BODY))
            categories = [
                " ".join(c.get_text(" ", strip=True).split())
                for c in entry.find_all("category")
                if isinstance(c, Tag)
            ]

            fields: dict[str, str] = {}
            if published:
                fields["published"] = published
            if guid:
                fields["guid"] = guid
            if categories:
                fields["category"] = ", ".join(c for c in categories if c)

            identity = guid or link or f"{title}\n{published}"
            items.append(
                Item(
                    key=stable_hash(identity),
                    target=target.name,
                    title=title,
                    url=link,
                    text=body,
                    fields=fields,
                )
            )

        return self.filter(target, items)
