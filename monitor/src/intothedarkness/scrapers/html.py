"""HTML scrapers: repeating-item extraction and whole-page change watching."""

from __future__ import annotations

from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Item, Selectors, Target, stable_hash
from .base import Scraper, register


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "lxml")


def _text_of(node: Tag | None) -> str:
    return " ".join(node.get_text(" ", strip=True).split()) if node else ""


def _pick(scope: Tag, selector: str | None) -> Tag | None:
    if not selector:
        return None
    return scope.select_one(selector)


def _link_of(scope: Tag, selector: str | None, base_url: str) -> str:
    node = _pick(scope, selector) if selector else scope.select_one("a[href]")
    if node is None:
        return ""
    href = node.get("href") or ""
    if isinstance(href, list):
        href = href[0] if href else ""
    return urljoin(base_url, href) if href else ""


@register
class CssScraper(Scraper):
    """Extract repeating records with CSS selectors.

    ``selectors.item`` picks the containers; ``title``/``link``/``text``/``key``
    and any ``attrs`` are read within each container. Item identity comes from
    ``key`` if given, else the link, else a hash of the title and text — so a
    listing whose ordering shuffles does not read as churn.
    """

    name = "css"

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            target.headers,
            target.params,
            target.body,
            network=target.network,
        )
        soup = _soup(resp.text)
        sel: Selectors = target.selectors

        if not sel.item:
            raise ValueError(
                f"target {target.name!r} uses the css scraper but sets no "
                "selectors.item; use scraper: page to watch a whole page"
            )

        items: list[Item] = []
        for node in soup.select(sel.item):
            title = _text_of(_pick(node, sel.title)) if sel.title else _text_of(node)
            link = _link_of(node, sel.link, resp.url)
            text = _text_of(_pick(node, sel.text)) if sel.text else ""

            fields: dict[str, str] = {}
            for field_name, spec in sel.attrs.items():
                selector, _, attr = spec.partition("@")
                sub = node.select_one(selector) if selector else node
                if sub is None:
                    continue
                if attr:
                    # A multi-valued attribute (class, rel) comes back as a list.
                    value = sub.get(attr)
                    if isinstance(value, list):
                        fields[field_name] = " ".join(value)
                    else:
                        fields[field_name] = value or ""
                else:
                    fields[field_name] = _text_of(sub)

            if sel.key:
                key_source = _text_of(_pick(node, sel.key))
                key = stable_hash(key_source) if key_source else ""
            else:
                key = ""
            if not key:
                key = stable_hash(link) if link else stable_hash(title, text)

            items.append(
                Item(
                    key=key,
                    target=target.name,
                    title=title,
                    url=link,
                    text=text,
                    fields=fields,
                )
            )

        return self.filter(target, items)


@register
class PageScraper(Scraper):
    """Watch one page (or one region of it) as a single item.

    Yields exactly one item with a fixed key, so every content change shows up
    as a CHANGED finding rather than a new record.
    """

    name = "page"

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            target.headers,
            target.params,
            target.body,
            network=target.network,
        )
        soup = _soup(resp.text)

        region = target.selectors.text or target.selectors.item
        if region:
            nodes = soup.select(region)
            if not nodes:
                raise ValueError(
                    f"target {target.name!r}: selector {region!r} matched nothing"
                )
            body = "\n".join(_text_of(n) for n in nodes)
        else:
            for junk in soup(["script", "style", "noscript"]):
                junk.decompose()
            body = _text_of(soup.body or soup)

        title = _text_of(soup.title) or target.name
        item = Item(
            key=stable_hash(target.name, target.url),
            target=target.name,
            title=title,
            url=resp.url,
            text=body,
            fields={"length": len(body)},
        )
        return self.filter(target, [item])
