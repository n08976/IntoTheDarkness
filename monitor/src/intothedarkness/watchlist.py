"""The priority watchlist: vendor names whose appearance anywhere is urgent.

A match bypasses the sector rules -- a vendor on any leak site, in any
sector, is a third-party exposure -- and goes out at once rather than with
the next scheduled report.

Names are matched, not domains, because that is what the list holds. Leak
sites spell names loosely ("Fresenius Medical Care US", "R1 RCM, Inc."),
so matching normalises both sides: case, punctuation and corporate suffixes
are ignored, and a vendor matches a victim whose name *starts with* the
vendor's -- but only a multi-word vendor. A single word as a prefix is how
"Olympus" fires on "Olympus Financial" and "GE" on half a leak site, so a
one-word vendor must equal the victim's whole name. Measured on every
victim observed to date, that rule kept the four real hits and dropped the
one wrong one.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

from .models import Finding

log = logging.getLogger(__name__)

SUFFIXES = {
    "inc", "incorporated", "llc", "llp", "lp", "ltd", "limited", "corp", "corporation",
    "co", "company", "gmbh", "ag", "plc", "sa", "srl", "usa", "us", "america", "americas",
    "group", "holdings",
}
_ALIAS_PREFIX = re.compile(r"^(formerly|previously|dba|d/b/a|a\s+division\s+of|now)\s+", re.I)
_PAREN = re.compile(r"\(([^)]*)\)")
_TOKEN = re.compile(r"[a-z0-9]+")


def normalise(name: str) -> tuple[str, ...]:
    """Lowercase tokens with punctuation and corporate suffixes removed."""
    tokens = [t for t in _TOKEN.findall(name.lower().replace("&", " and "))]
    while tokens and tokens[-1] in SUFFIXES:
        tokens.pop()
    return tuple(tokens)


@dataclass(frozen=True, slots=True)
class Vendor:
    name: str                      # as written in the file
    forms: tuple[tuple[str, ...], ...]   # normalised name plus any aliases

    def matches(self, victim: str) -> bool:
        v = normalise(victim)
        if not v:
            return False
        for form in self.forms:
            if not form:
                continue
            if form == v:
                return True
            if len(form) > 1 and v[: len(form)] == form:
                return True
        return False


# A headline names a vendor incidentally all the time ("Microsoft patches
# Excel"). One of these words alongside the name is what makes it a match.
INCIDENT_WORDS = {
    "breach", "breached", "ransomware", "leak", "leaked", "leaks", "hack", "hacked",
    "hackers", "attack", "attacked", "cyberattack", "exposed", "exposure", "stolen",
    "extortion", "compromised", "compromise", "intrusion", "incident", "victim",
    "victims", "dump", "dumped", "infostealer", "credentials", "claims", "claimed",
}
HEADLINE_MIN_WORD = 5   # "Dell", "GE", "Lap" inside a sentence are not a mention


def _contains(seq: tuple[str, ...], sub: tuple[str, ...]) -> bool:
    n = len(sub)
    return n > 0 and any(seq[i : i + n] == sub for i in range(len(seq) - n + 1))


def mentioned_in(vendor: Vendor, headline: str) -> bool:
    """A headline names the vendor and is about an incident.

    Multi-word names may appear anywhere in the sentence; a single word must
    be at least HEADLINE_MIN_WORD letters, or common short names would fire
    on half the news. Corporate suffixes are stripped from the vendor only:
    a headline keeps its words.
    """
    tokens = tuple(_TOKEN.findall(headline.lower().replace("&", " and ")))
    if not tokens or not (set(tokens) & INCIDENT_WORDS):
        return False
    for form in vendor.forms:
        if not form:
            continue
        if len(form) == 1 and len(form[0]) < HEADLINE_MIN_WORD:
            continue
        if _contains(tokens, form):
            return True
    return False


def parse(text: str) -> list[Vendor]:
    """One vendor per line; duplicates collapse; parentheticals become aliases.

    "Oracle (formerly Cerner)" matches both "Oracle" and "Cerner";
    "Bamboo Health (previously Patient Ping)" both of those.
    """
    seen: dict[tuple[tuple[str, ...], ...], Vendor] = {}
    for raw in text.splitlines():
        line = raw.strip().strip('"').strip()
        if not line or line.startswith("#"):
            continue
        aliases = [_ALIAS_PREFIX.sub("", m.strip()) for m in _PAREN.findall(line)]
        base = _PAREN.sub(" ", line).strip()
        forms = tuple(
            f for f in (normalise(base), *(normalise(a) for a in aliases)) if f
        )
        if not forms:
            continue
        seen.setdefault(forms, Vendor(name=base, forms=forms))
    return list(seen.values())


def load(path: Path) -> list[Vendor]:
    if not path.exists():
        return []
    return parse(path.read_text(encoding="utf-8", errors="replace"))


def refresh(path: Path, url: str, fetch) -> list[Vendor]:  # noqa: ANN001 - callable
    """Pull the latest list from ``url`` into ``path``; on failure, use the cache.

    The list is edited in a git repository and pushed; the monitor runs on a
    box with no checkout of it, so it reads the published file each run.
    """
    if url:
        try:
            text = fetch(url)
            if text and text.strip():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
        except Exception as exc:
            log.warning("watchlist refresh failed, using cached copy: %s", exc)
    return load(path)


def find_matches(
    vendors: list[Vendor],
    findings: list[Finding],
    headline_targets: set[str] | None = None,
) -> dict[int, Vendor]:
    """index of finding -> the vendor it names.

    Findings from ``headline_targets`` are news: their titles are sentences,
    matched by mention plus an incident word rather than as victim names.
    """
    hits: dict[int, Vendor] = {}
    heads = headline_targets or set()
    for i, f in enumerate(findings):
        title = f.item.title if f.item else ""
        as_headline = f.target in heads
        for vendor in vendors:
            hit = mentioned_in(vendor, title) if as_headline else vendor.matches(title)
            if hit:
                hits[i] = vendor
                break
    return hits
