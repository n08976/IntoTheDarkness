"""Propose CSS selectors for a listing page.

Leak sites all differ, and hand-writing selectors for each is the slow part of
onboarding one. This looks for the repeated structure that a listing is made of
and suggests an ``item``/``title`` pair, with a sample of what it would extract
so the result can be judged at a glance rather than trusted.

It returns ranked candidates for a human to choose between, deliberately: it is
reliable on the card, table and list layouts leak sites use, and weakest on
deeply nested table markup where every row is a ``tr`` and structure carries no
meaning. Read the samples, not the ranking.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from bs4 import BeautifulSoup, Tag

# A listing has many entries but is not a wall of tiny nodes.
MIN_REPEATS = 3
MAX_REPEATS = 2000
# Victim names are short; paragraphs of prose are not names.
TITLE_MAX_CHARS = 120


@dataclass
class Suggestion:
    item: str
    title: str | None
    count: int
    score: float
    samples: list[str] = field(default_factory=list)

    def as_yaml(self) -> str:
        lines = ["selectors:", f'  item: "{self.item}"']
        if self.title:
            lines.append(f'  title: "{self.title}"')
        return "\n".join(lines)


def _signature(tag: Tag) -> str | None:
    """A stable identity for 'elements that look like each other'."""
    # BeautifulSoup returns a list for multi-valued attributes, a str otherwise.
    raw = tag.get("class")
    if isinstance(raw, str):
        classes: list[str] = raw.split()
    elif raw:
        classes = [str(c) for c in raw]
    else:
        classes = []
    # Class names carrying digits are usually per-item, not structural.
    classes = [c for c in classes if c and not re.search(r"\d{2,}", c)]
    if classes:
        return f"{tag.name}.{'.'.join(sorted(classes)[:3])}"
    if tag.name in ("tr", "li", "article", "section"):
        return tag.name
    return None


def _selector_for(signature: str) -> str:
    return signature


def _text(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


def _shape(tag: Tag) -> frozenset[str]:
    """The set of child signatures directly under a node."""
    return frozenset(
        _signature(child) or child.name
        for child in tag.find_all(True, recursive=False)
    )


def _uniformity(nodes: list[Tag]) -> float:
    """How alike the candidate entries are, 0..1.

    A real listing repeats one shape. A bare tag like ``tr`` also matches the
    nav row and spacer rows, which is exactly what this demotes.
    """
    if not nodes:
        return 0.0
    shapes = Counter(_shape(n) for n in nodes)
    return shapes.most_common(1)[0][1] / len(nodes)


def _title_candidates(nodes: list[Tag]) -> list[tuple[str, float, list[str]]]:
    """Rank child selectors by how much they look like a name column."""
    counts: dict[str, list[str]] = defaultdict(list)
    # How many *entries* contain this selector at all — distinct from how many
    # times it occurs. A row class repeated three times per entry must not score
    # as three times the coverage, or it outranks the one-per-entry heading that
    # actually holds the name.
    entries_with: dict[str, int] = defaultdict(int)

    for node in nodes:
        seen_here: set[str] = set()
        for child in node.find_all(True, recursive=True):
            signature = _signature(child) or child.name
            if signature in ("script", "style"):
                continue
            text = _text(child)
            if not text or len(text) > TITLE_MAX_CHARS:
                continue
            counts[signature].append(text)
            seen_here.add(signature)
        for signature in seen_here:
            entries_with[signature] += 1

    ranked: list[tuple[str, float, list[str]]] = []
    for signature, values in counts.items():
        coverage = entries_with[signature] / max(1, len(nodes))
        if coverage < 0.5:
            continue  # must appear in most entries
        # Selectors occurring several times per entry are rows, not names.
        repeats = len(values) / max(1, entries_with[signature])
        if repeats > 1.5:
            continue
        distinct = len(set(values)) / max(1, len(values))
        lengths = [len(v) for v in values]
        mean_len = sum(lengths) / len(lengths)
        # Prefer selectors present in every entry, mostly distinct, short-ish,
        # and biased toward heading and anchor tags.
        score = coverage * 2 + distinct * 3 - abs(mean_len - 32) / 100
        # Navigation and ordinals ("new", "1.") are not organisation names.
        if mean_len < 8:
            score -= 2.0
        if re.match(r"^h[1-6]\b", signature):
            score += 1.0
        elif signature.startswith("a"):
            score += 0.6
        if any(word in signature.lower() for word in ("title", "name", "company", "victim")):
            score += 1.2
        ranked.append((signature, score, values[:5]))

    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked


def suggest(html: str, limit: int = 3) -> list[Suggestion]:
    """Return the most plausible ``item``/``title`` selector pairs."""
    soup = BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    signatures: Counter[str] = Counter()
    by_signature: dict[str, list[Tag]] = defaultdict(list)

    for tag in soup.find_all(True):
        signature = _signature(tag)
        if not signature:
            continue
        signatures[signature] += 1
        by_signature[signature].append(tag)

    suggestions: list[Suggestion] = []
    for signature, count in signatures.items():
        if not MIN_REPEATS <= count <= MAX_REPEATS:
            continue
        nodes = by_signature[signature]

        # Skip containers whose entries are mostly empty or enormous.
        texts = [_text(n) for n in nodes]
        non_empty = [t for t in texts if t]
        if len(non_empty) < len(nodes) * 0.6:
            continue
        mean_len = sum(len(t) for t in non_empty) / max(1, len(non_empty))
        if mean_len > 2000:
            continue

        # A container nested inside another equally-repeated one is usually the
        # inner detail, not the entry; prefer the outer.
        depth = len(list(nodes[0].parents))

        titles = _title_candidates(nodes)
        title_sel, title_score, samples = titles[0] if titles else (None, 0.0, [])
        if not samples:
            samples = [t[:80] for t in non_empty[:5]]

        distinct = len(set(non_empty)) / max(1, len(non_empty))
        uniformity = _uniformity(nodes)
        score = (
            min(count, 200) / 40      # repetition, capped
            + distinct * 2            # entries should differ from one another
            + title_score             # and contain a name-shaped child
            - depth / 30              # prefer the outer container
        )
        # Entries of a real listing share a shape. Scale by that, so a bare tag
        # matching a nav row plus content rows is demoted below the tight match.
        score *= 0.4 + 0.6 * uniformity
        # A class-bearing selector is a deliberate structure; a bare tag name
        # matches incidental layout too.
        if "." in signature:
            score += 1.5
        suggestions.append(
            Suggestion(
                item=_selector_for(signature),
                title=title_sel,
                count=count,
                score=round(score, 2),
                samples=samples[:5],
            )
        )

    suggestions.sort(key=lambda s: s.score, reverse=True)
    return suggestions[:limit]
