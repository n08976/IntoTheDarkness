"""Collapse discovered onion addresses into proposals for the curated list.

Scraped pages leak links to other hidden services — mirrors, sister sites,
directories. Those accumulate in findings as extracted indicators. This gathers
them, drops what is already listed or unusable, and ranks what is left so a
short review produces a commit rather than a pile of raw strings.

Nothing here writes to the list; it proposes, and a person decides.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from ..storage.db import FindingRow
from ..tor import ONION_V3_RE, host_of
from .store import Bookmarks, Link, guess_category

# urlparse happily reports "not a url" as a hostname, so validate the shape:
# dotted labels, or a bare IPv4 address.
HOSTNAME_RE = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$", re.I
)
IPV4_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")


def valid_host(host: str) -> bool:
    if not host or len(host) > 253 or " " in host:
        return False
    if host.endswith(".onion"):
        return bool(ONION_V3_RE.match(host))
    return bool(HOSTNAME_RE.match(host) or IPV4_RE.match(host))


@dataclass
class Proposal:
    """A candidate address, with the evidence for adding it."""

    host: str
    url: str
    seen: int = 0
    sources: set[str] = field(default_factory=set)
    contexts: list[str] = field(default_factory=list)
    first_seen: datetime | None = None
    suggested_title: str = ""
    suggested_category: str = ""

    @property
    def score(self) -> float:
        """More sightings across more distinct sources is stronger evidence."""
        return self.seen + len(self.sources) * 2

    def to_link(self) -> Link:
        return Link(title=self.suggested_title or self.host[:16], url=self.url)


def _title_from(host: str, contexts: list[str]) -> str:
    """Guess a human label: the shortest distinctive context, else the address."""
    candidates = [
        " ".join(c.split())
        for c in contexts
        if c and 3 <= len(c.strip()) <= 60 and host[:12] not in c
    ]
    if not candidates:
        return host[:16]
    return min(candidates, key=len)


def from_findings(
    rows: list[FindingRow],
    bookmarks: Bookmarks,
    min_sightings: int = 1,
) -> list[Proposal]:
    """Build proposals from stored findings, excluding what is already listed."""
    known = bookmarks.hosts()
    proposals: dict[str, Proposal] = {}
    contexts: dict[str, list[str]] = defaultdict(list)

    for row in rows:
        payload = row.payload or {}
        item = payload.get("item") or {}
        fields = item.get("fields") or {}
        onions = (fields.get("iocs") or {}).get("onion") or []

        for raw in onions:
            host = host_of(f"http://{raw}") or str(raw).lower()
            if not ONION_V3_RE.match(host) or host in known:
                continue

            proposal = proposals.get(host)
            if proposal is None:
                proposal = Proposal(host=host, url=f"http://{host}/")
                proposals[host] = proposal

            proposal.seen += 1
            proposal.sources.add(row.target)
            created = row.created_at
            if proposal.first_seen is None or (created and created < proposal.first_seen):
                proposal.first_seen = created

            title = item.get("title") or ""
            if title:
                contexts[host].append(str(title))

    for host, proposal in proposals.items():
        proposal.contexts = contexts[host][:5]
        proposal.suggested_title = _title_from(host, proposal.contexts)
        proposal.suggested_category = guess_category(proposal.url, proposal.suggested_title)

    out = [p for p in proposals.values() if p.seen >= min_sightings]
    out.sort(key=lambda p: (-p.score, p.host))
    return out


def from_urls(urls: list[str], bookmarks: Bookmarks) -> list[Proposal]:
    """Proposals from addresses handed over directly, deduplicated and validated."""
    known = bookmarks.hosts()
    counts: Counter[str] = Counter()
    order: list[str] = []

    for raw in urls:
        candidate = raw.strip()
        if not candidate:
            continue
        if "://" not in candidate:
            candidate = f"http://{candidate}"
        host = host_of(candidate)
        if not valid_host(host) or host in known:
            continue
        if host not in counts:
            order.append(host)
        counts[host] += 1

    proposals = []
    for host in order:
        proposal = Proposal(
            host=host,
            url=f"http://{host}/",
            seen=counts[host],
            sources={"manual"},
            suggested_title=host[:16],
        )
        proposal.suggested_category = guess_category(proposal.url)
        proposals.append(proposal)
    return proposals


def rejected(urls: list[str], bookmarks: Bookmarks) -> dict[str, str]:
    """Why each supplied address was not proposed — duplicates, v2, malformed."""
    known = bookmarks.hosts()
    out: dict[str, str] = {}
    for raw in urls:
        candidate = raw.strip()
        if not candidate:
            continue
        full = candidate if "://" in candidate else f"http://{candidate}"
        host = host_of(full)
        if host in known:
            out[candidate] = "already in the list"
        elif host.endswith(".onion") and not ONION_V3_RE.match(host):
            out[candidate] = "not a valid v3 onion address"
        elif not valid_host(host):
            out[candidate] = "not a usable hostname"
    return out
