"""Combine what several sources know about one victim into one entry.

Aggregators disagree about links: one gives the leak post, one a dossier page
of its own, one only its site root, one nothing. When they all name the same
victim, the report should carry the best link in the main slot and the rest
alongside, rather than whichever happened to arrive first.
"""

from __future__ import annotations

from urllib.parse import urlparse

from ..models import Finding


def is_weak(url: str) -> bool:
    """A site root says nothing about this victim; anything deeper might."""
    if not url:
        return True
    parsed = urlparse(url)
    return parsed.path in ("", "/") and not parsed.query and not parsed.fragment


def source_label(target: str) -> str:
    """'agg-darkfield' -> 'darkfield': the part of a target name a reader knows."""
    return target.split("-", 1)[1] if "-" in target else target


def merge_links(into: Finding, other: Finding) -> None:
    """Fold ``other``'s links into ``into``, which keeps its own date and text."""
    a, b = into.item, other.item
    if a is None or b is None:
        return
    if b.url and (is_weak(a.url) and not is_weak(b.url) or not a.url):
        # Promote a real page over a bare root; remember the old one below.
        if a.url:
            _remember(a, source_label(into.target), a.url)
        a.url = b.url
    elif b.url and not is_weak(b.url) and b.url != a.url:
        _remember(a, source_label(other.target), b.url)
    if not a.fields.get("website") and b.fields.get("website"):
        a.fields["website"] = b.fields["website"]
    for name in ("group", "country", "sector"):
        if not a.fields.get(name) and b.fields.get(name):
            a.fields[name] = b.fields[name]


def _remember(item, label: str, url: str) -> None:  # noqa: ANN001 - Item
    also = item.fields.setdefault("also", [])
    if url != item.url and all(entry["url"] != url for entry in also):
        also.append({"source": label, "url": url})
