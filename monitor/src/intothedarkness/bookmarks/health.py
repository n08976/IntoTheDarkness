"""Liveness checking for the curated list.

Onion addresses for threat-actor sites rotate and go dark constantly, which is
the single thing that makes a hand-maintained bookmark list rot. This checks
each entry over Tor and records the result, so "verify before relying on any
single one" becomes a command rather than a manual chore.

Results are stored in our own database, never written back into
``bookmarks.json`` unless explicitly asked for — the list stays the human's file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlparse

from ..config import Settings, get_settings
from ..scrapers.fetch import Fetcher, Network
from ..tor import ONION_V3_RE, host_of, is_onion, redact, validate_onion
from .store import Bookmarks, Link

log = logging.getLogger(__name__)

ALIVE = "alive"
DEAD = "dead"
INVALID = "invalid"
SKIPPED = "skipped"


@dataclass(slots=True)
class Health:
    title: str
    url: str
    category: str
    status: str
    detail: str = ""
    status_code: int | None = None
    bytes_len: int = 0
    title_seen: str = ""
    checked_at: datetime | None = None

    @property
    def onion(self) -> bool:
        return is_onion(self.url)

    def display_url(self, redact_onions: bool = True) -> str:
        return redact(self.url, redact_onions)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "url": self.url,
            "category": self.category,
            "status": self.status,
            "detail": self.detail,
            "status_code": self.status_code,
            "bytes": self.bytes_len,
            "page_title": self.title_seen,
            "checked_at": (self.checked_at or datetime.now(UTC)).isoformat(),
        }


def _page_title(html: str) -> str:
    from bs4 import BeautifulSoup

    try:
        soup = BeautifulSoup(html[:200_000], "lxml")
        return " ".join((soup.title.get_text() if soup.title else "").split())[:120]
    except Exception:
        return ""


def check_link(
    link: Link, category: str, fetcher: Fetcher, settings: Settings | None = None
) -> Health:
    """Check one entry. A dead onion is a normal state, not an error."""
    settings = settings or get_settings()
    result = Health(
        title=link.title,
        url=link.url,
        category=category,
        status=SKIPPED,
        checked_at=datetime.now(UTC),
    )

    scheme = urlparse(link.url).scheme.lower()
    if scheme not in ("http", "https"):
        # Deliberate non-HTTP entries exist: `about:manual` is a Tor Browser
        # internal page, `tonsite://` needs a TON gateway. Neither is reachable
        # over HTTP, and neither is broken.
        result.status = SKIPPED
        result.detail = f"{scheme or 'no'}: scheme is not fetchable over HTTP"
        return result

    if link.is_onion:
        shape = validate_onion(link.url)
        if not shape.ok:
            result.status = INVALID
            result.detail = shape.reason
            return result
        network: str | Network = Network.TOR
    else:
        network = Network.AUTO

    try:
        resp = fetcher.request("GET", link.url, network=network)
    except Exception as exc:
        result.status = DEAD
        result.detail = redact(str(exc), settings.redact_onion_in_logs)[:200]
        return result

    result.status = ALIVE
    result.status_code = resp.status
    result.bytes_len = len(resp.content)
    result.title_seen = _page_title(resp.text)
    return result


def check_all(
    bookmarks: Bookmarks,
    fetcher: Fetcher,
    onion_only: bool = False,
    category: str | None = None,
    settings: Settings | None = None,
    progress=None,
) -> list[Health]:
    """Check every entry, or a filtered subset."""
    settings = settings or get_settings()
    results: list[Health] = []

    for cat, link in bookmarks.links:
        if category and cat.name.lower() != category.lower():
            continue
        if onion_only and not link.is_onion:
            continue
        result = check_link(link, cat.name, fetcher, settings)
        results.append(result)
        if progress is not None:
            progress(result)

    return results


def summarize(results: list[Health]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


def dead_entries(results: list[Health]) -> list[Health]:
    return [r for r in results if r.status in (DEAD, INVALID)]


def stale_v2_or_malformed(bookmarks: Bookmarks) -> list[tuple[str, Link, str]]:
    """Entries whose address cannot resolve, found without any network access."""
    out = []
    for cat, link in bookmarks.links:
        if not link.is_onion:
            continue
        host = host_of(link.url)
        if ONION_V3_RE.match(host):
            continue
        out.append((cat.name, link, validate_onion(link.url).reason))
    return out
