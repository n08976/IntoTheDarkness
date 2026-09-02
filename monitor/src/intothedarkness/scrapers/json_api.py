"""JSON API scraper, for targets that expose data without HTML."""

from __future__ import annotations

from typing import Any

from ..models import Item, Target, stable_hash
from .base import Scraper, register


def dig(data: Any, path: str | None) -> Any:
    """Walk a dotted path, where a numeric segment indexes into a list."""
    if not path:
        return data
    current = data
    for part in path.split("."):
        if current is None:
            return None
        if part.isdigit() and isinstance(current, list):
            index = int(part)
            current = current[index] if index < len(current) else None
        elif isinstance(current, dict):
            current = current.get(part)
        else:
            return None
    return current


@register
class JsonScraper(Scraper):
    """Read a list of records from a JSON endpoint.

    ``json_path`` points at the list; ``json_fields`` maps output field names to
    dotted paths within each record. The reserved output names ``title``,
    ``url``, ``text`` and ``key`` populate the item itself.
    """

    name = "json"

    RESERVED = ("title", "url", "text", "key")

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            {"Accept": "application/json", **target.headers},
            target.params,
            target.body,
            network=target.network,
        )
        payload = resp.json()
        records = dig(payload, target.json_path)

        if records is None:
            raise ValueError(
                f"target {target.name!r}: json_path {target.json_path!r} matched nothing"
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
                url = values.get("url")
                key = stable_hash(str(url)) if url else stable_hash(repr(sorted(extra.items())))

            items.append(
                Item(
                    key=key,
                    target=target.name,
                    title=str(values.get("title") or ""),
                    url=str(values.get("url") or ""),
                    text=str(values.get("text") or ""),
                    fields=extra,
                )
            )

        return self.filter(target, items)
