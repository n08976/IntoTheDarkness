"""Read and write the curated ``bookmarks.json`` list.

That file is the source of truth for what is worth watching, and it is
hand-maintained and committed to git. So this module preserves its authored
style exactly — two-space structure with each link collapsed onto one line —
because a formatter that reflows the file turns a one-link addition into a
four-hundred-line diff nobody can review.

The companion ``generate.py`` reads only ``title`` and ``url`` from each link,
so additional keys are carried through harmlessly; they are still opt-in.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from ..tor import ONION_V3_RE, host_of, is_onion

LINK_KEYS = ("title", "url")


@dataclass
class Link:
    title: str
    url: str
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def is_onion(self) -> bool:
        return is_onion(self.url)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": self.title, "url": self.url}
        out.update(self.extra)
        return out


@dataclass
class Category:
    name: str
    note: str = ""
    links: list[Link] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name}
        if self.note:
            out["note"] = self.note
        out["links"] = [link.to_dict() for link in self.links]
        return out


@dataclass
class Bookmarks:
    title: str = ""
    description: str = ""
    categories: list[Category] = field(default_factory=list)

    # ------------------------------------------------------------------ loading

    @classmethod
    def from_dict(cls, data: Any) -> Bookmarks:
        if not isinstance(data, dict) or "categories" not in data:
            raise ValueError("bookmarks.json must be an object with a 'categories' list")

        categories = []
        for entry in data.get("categories") or []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            links = []
            for raw in entry.get("links") or []:
                if not isinstance(raw, dict) or not raw.get("url"):
                    continue
                links.append(
                    Link(
                        title=str(raw.get("title") or raw["url"]),
                        url=str(raw["url"]),
                        extra={k: v for k, v in raw.items() if k not in LINK_KEYS},
                    )
                )
            categories.append(
                Category(name=str(entry["name"]), note=str(entry.get("note") or ""), links=links)
            )

        return cls(
            title=str(data.get("title") or ""),
            description=str(data.get("description") or ""),
            categories=categories,
        )

    @classmethod
    def load(cls, path: Path) -> Bookmarks:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "description": self.description,
            "categories": [c.to_dict() for c in self.categories],
        }

    # ------------------------------------------------------------------ querying

    @property
    def links(self) -> list[tuple[Category, Link]]:
        return [(c, link) for c in self.categories for link in c.links]

    def category(self, name: str) -> Category | None:
        lowered = name.lower()
        return next((c for c in self.categories if c.name.lower() == lowered), None)

    def hosts(self) -> set[str]:
        """Every host already present, for deduplicating proposals."""
        return {link.host for _, link in self.links if link.host}

    def has(self, url: str) -> bool:
        """Whether this address is already listed, ignoring scheme and path."""
        host = host_of(url)
        return bool(host) and host in self.hosts()

    def counts(self) -> dict[str, int]:
        onion = sum(1 for _, link in self.links if link.is_onion)
        total = len(self.links)
        return {
            "categories": len(self.categories),
            "links": total,
            "onion": onion,
            "clearnet": total - onion,
        }

    # ------------------------------------------------------------------ mutation

    def add(self, category: str, link: Link, note: str = "") -> bool:
        """Add a link unless its host is already listed. Returns whether it was added."""
        if self.has(link.url):
            return False
        target = self.category(category)
        if target is None:
            target = Category(name=category, note=note)
            self.categories.append(target)
        target.links.append(link)
        return True


# ------------------------------------------------------------------- formatting


def dumps(bookmarks: Bookmarks, compact_links: bool = True) -> str:
    """Serialise in the file's authored style.

    ``compact_links`` keeps each link on a single line, which is how the file is
    written by hand and what keeps diffs readable.
    """
    data = bookmarks.to_dict()
    if not compact_links:
        return json.dumps(data, indent=2, ensure_ascii=False) + "\n"

    def enc(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False)

    lines = ["{"]
    lines.append(f"  {enc('title')}: {enc(data['title'])},")
    lines.append(f"  {enc('description')}: {enc(data['description'])},")
    lines.append(f"  {enc('categories')}: [")

    categories = data["categories"]
    for cat_index, category in enumerate(categories):
        lines.append("    {")
        lines.append(f"      {enc('name')}: {enc(category['name'])},")
        if category.get("note"):
            lines.append(f"      {enc('note')}: {enc(category['note'])},")
        lines.append(f"      {enc('links')}: [")

        links = category["links"]
        for link_index, link in enumerate(links):
            body = ", ".join(f"{enc(k)}: {enc(v)}" for k, v in link.items())
            comma = "," if link_index < len(links) - 1 else ""
            lines.append(f"        {{ {body} }}{comma}")

        lines.append("      ]")
        lines.append("    }" + ("," if cat_index < len(categories) - 1 else ""))

    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def save(bookmarks: Bookmarks, path: Path, compact_links: bool = True) -> None:
    Path(path).write_text(dumps(bookmarks, compact_links), encoding="utf-8")


def guess_category(url: str, title: str = "") -> str:
    """Best-guess category for a newly discovered address."""
    haystack = f"{title} {url}".lower()
    if any(w in haystack for w in ("search", "directory", "index", "wiki", "links")):
        return "Directories & Search Engines"
    if any(w in haystack for w in ("forum", "board", "community")):
        return "Forums & Communities"
    if any(w in haystack for w in ("market", "shop", "vendor")):
        return "Marketplaces"
    if any(w in haystack for w in ("torproject", "tor project", "bridges")):
        return "Tor Project & Infrastructure"
    return "Ransomware & Extortion Leak Sites"


def valid_onion(url: str) -> bool:
    return bool(ONION_V3_RE.match(host_of(url))) if is_onion(url) else bool(urlparse(url).netloc)
