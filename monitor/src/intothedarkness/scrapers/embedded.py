"""Read data that a JavaScript app embedded in its own HTML.

A page whose ``<body>`` is empty is not necessarily a page that needs a browser.
Most single-page apps ship their initial state as JSON inside the HTML they
already served — Inertia uses ``<script data-page>``, Next.js uses
``__NEXT_DATA__``, Nuxt assigns ``window.__NUXT__``. The victim list is right
there in the bytes we already fetched.

That matters beyond convenience: running a headless browser to scrape a hidden
service means a second network stack that has to be routed through Tor
correctly, with its own fingerprint and its own leak surface. Parsing JSON has
neither. Reach for a browser only when the data genuinely is not in the HTML.
"""

from __future__ import annotations

import json
import logging
import re
from html import unescape
from typing import Any

from bs4 import BeautifulSoup

from ..models import Item, Target, stable_hash
from .base import Scraper, register
from .json_api import dig

log = logging.getLogger(__name__)

# `window.__NUXT__ = {...}` / `var __data = {...}` style assignments.
ASSIGNMENT_RE = re.compile(
    r"(?:window\.)?(__[A-Z0-9_]+__|__NEXT_DATA__)\s*=\s*(\{)", re.I
)


def _attr(tag, name: str) -> str:
    """An attribute as a plain string, flattening multi-valued attributes."""
    value = tag.get(name)
    if isinstance(value, list):
        return " ".join(str(v) for v in value)
    return str(value) if value else ""


def _balanced(text: str, start: int) -> str | None:
    """Slice the JSON object beginning at ``start``, respecting strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def extract_payloads(html: str) -> list[tuple[str, Any]]:
    """Every JSON blob the page embedded, as (source label, parsed value)."""
    found: list[tuple[str, Any]] = []
    soup = BeautifulSoup(html, "lxml")

    for tag in soup.find_all("script"):
        # BeautifulSoup returns a list for multi-valued attributes.
        script_type = _attr(tag, "type").lower()
        identifier = _attr(tag, "id") or _attr(tag, "data-page")
        body = tag.string or tag.get_text() or ""
        if not body.strip():
            continue

        if script_type in ("application/json", "application/ld+json"):
            try:
                found.append((identifier or script_type, json.loads(body)))
            except json.JSONDecodeError:
                log.debug("script %r held unparseable JSON", identifier)
            continue

        # Inline assignment of an app-state object.
        match = ASSIGNMENT_RE.search(body)
        if match:
            blob = _balanced(body, match.start(2))
            if blob:
                try:
                    found.append((match.group(1), json.loads(blob)))
                except json.JSONDecodeError:
                    log.debug("assignment %r held unparseable JSON", match.group(1))

    # Inertia puts the payload in an attribute rather than the script body.
    for tag in soup.find_all(True):
        value = _attr(tag, "data-page")
        if value.strip().startswith("{"):
            try:
                found.append(("data-page", json.loads(unescape(value))))
            except json.JSONDecodeError:
                log.debug("data-page attribute held unparseable JSON")

    return found


def find_records(payloads: list[tuple[str, Any]], path: str | None) -> Any:
    """Resolve ``path`` against whichever payload contains it."""
    for _label, payload in payloads:
        value = dig(payload, path)
        if value is not None:
            return value
    return None


@register
class EmbeddedJsonScraper(Scraper):
    """Extract records from JSON a page embedded in its own HTML.

    Configure it like the ``json`` scraper — ``json_path`` locates the list and
    ``json_fields`` maps dotted paths onto item fields — but the source is the
    HTML document rather than an API response.
    """

    name = "embedded"

    RESERVED = ("title", "url", "text", "key")

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            target.headers,
            target.params,
            target.body,
            network=target.network,
        )

        payloads = extract_payloads(resp.text)
        if not payloads:
            raise ValueError(
                f"target {target.name!r}: no embedded JSON found — this page may "
                "genuinely require a browser"
            )

        records = find_records(payloads, target.json_path)
        if records is None:
            available = ", ".join(label for label, _ in payloads) or "(unnamed)"
            raise ValueError(
                f"target {target.name!r}: json_path {target.json_path!r} matched "
                f"nothing in the embedded payloads ({available})"
            )
        if isinstance(records, dict):
            records = [records]
        if not isinstance(records, list):
            raise ValueError(
                f"target {target.name!r}: json_path {target.json_path!r} is "
                f"{type(records).__name__}, expected a list"
            )

        mapping = target.json_fields or {"title": "title", "url": "url"}
        items: list[Item] = []

        for record in records:
            values = {name: dig(record, path) for name, path in mapping.items()}
            extra = {
                k: v for k, v in values.items() if k not in self.RESERVED and v is not None
            }

            key = values.get("key")
            key = stable_hash(str(key)) if key is not None else ""
            if not key:
                title = values.get("title")
                key = stable_hash(str(title)) if title else stable_hash(
                    repr(sorted(extra.items()))
                )

            # Sector resolution belongs to the pipeline; anything set here
            # would be a guess wearing the source's authority.
            title = str(values.get("title") or "")

            items.append(
                Item(
                    key=key,
                    target=target.name,
                    title=title,
                    url=str(values.get("url") or ""),
                    text=str(values.get("text") or ""),
                    fields=extra,
                )
            )

        return self.filter(target, items)
