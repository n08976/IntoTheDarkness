"""An authoritative sector index, built from a source that publishes one.

Keyword-matching a company name leaves most victims unlabelled — measured at
61% unknown across one leak site's 263 entries. Names like "Easterseals",
"Community Care Alliance" and "Florida Lung" are plainly healthcare to a human
and invisible to a keyword list.

ransomware.live publishes victims indexed by sector. Downloading those lists
once gives a lookup that turns a guess into a stated fact for any victim it
covers, marked as such so routing rules can tell the difference.

The API allows roughly one request per minute, so the index is cached on disk
and refreshed deliberately rather than during a monitoring run.
"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..config import Settings, get_settings
from .sector import DEFAULT_SECTORS, UNKNOWN, normalize_sector

log = logging.getLogger(__name__)

SECTOR_VICTIMS_URL = "https://api.ransomware.live/v2/sectorvictims/{sector}"

# Names too short or too generic to identify one organisation. "Summit",
# "Pioneer" and "United" collide across dozens of unrelated companies, and a
# wrong sector silently misroutes an alert.
# A single word must be long to be distinctive; multi-word names can be shorter
# but still need enough substance to identify one organisation.
MIN_SINGLE_WORD = 8
MIN_KEY_LENGTH = 6
GENERIC_NAMES = frozenset(
    {
        "unknown", "n a", "na", "none", "confidential", "undisclosed",
        "not disclosed", "anonymous", "redacted", "victim", "company",
    }
)


def index_key(name: str) -> str:
    """Normalised lookup key for a victim name."""
    text = unicodedata.normalize("NFKD", (name or "").lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Apostrophes are dropped rather than turned into spaces, so "St Joseph's"
    # and "St Josephs" produce the same key instead of failing to match.
    text = text.replace("'", "").replace("\u2019", "")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def usable_key(key: str) -> bool:
    """Whether a name is distinctive enough to match on safely."""
    if not key or key in GENERIC_NAMES:
        return False
    if len(key) < MIN_KEY_LENGTH:
        return False
    return not (len(key.split()) < 2 and len(key) < MIN_SINGLE_WORD)


@dataclass
class SectorIndex:
    """victim name -> sector, from a source that classifies them itself."""

    entries: dict[str, str] = field(default_factory=dict)
    domains: dict[str, str] = field(default_factory=dict)
    built_at: str = ""
    sectors: list[str] = field(default_factory=list)

    def __len__(self) -> int:
        return len(self.entries)

    def lookup(self, name: str, domain: str = "") -> str:
        """The indexed sector for this victim, or ``unknown``."""
        if domain:
            host = domain.strip().lower().removeprefix("www.").split("/")[0]
            found = self.domains.get(host)
            if found:
                return found
        key = index_key(name)
        if not usable_key(key):
            return UNKNOWN
        return self.entries.get(key, UNKNOWN)

    # ------------------------------------------------------------------- io

    @classmethod
    def path(cls, settings: Settings | None = None) -> Path:
        settings = settings or get_settings()
        return settings.data_dir / "sector-index.json"

    @classmethod
    def load(cls, settings: Settings | None = None) -> SectorIndex:
        path = cls.path(settings)
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            log.warning("sector index at %s is unreadable; ignoring", path)
            return cls()
        return cls(
            entries=data.get("entries") or {},
            domains=data.get("domains") or {},
            built_at=data.get("built_at") or "",
            sectors=data.get("sectors") or [],
        )

    def save(self, settings: Settings | None = None) -> Path:
        settings = settings or get_settings()
        settings.ensure_dirs()
        path = self.path(settings)
        path.write_text(
            json.dumps(
                {
                    "built_at": self.built_at,
                    "sectors": self.sectors,
                    "entries": self.entries,
                    "domains": self.domains,
                },
                indent=0,
            ),
            encoding="utf-8",
        )
        return path


def build(
    sectors: list[str] | None = None,
    existing: SectorIndex | None = None,
    delay: float = 65.0,
    timeout: float = 120.0,
    progress=None,
) -> SectorIndex:
    """Download sector victim lists and fold them into an index.

    ``delay`` paces requests: the API permits about one a minute, and hammering
    it returns a rate-limit message rather than data.
    """
    from ..models import utcnow

    wanted = sectors or list(DEFAULT_SECTORS)
    index = existing or SectorIndex()
    seen_sectors = set(index.sectors)

    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        for position, sector in enumerate(wanted):
            if position:
                time.sleep(delay)
            url = SECTOR_VICTIMS_URL.format(sector=sector)
            try:
                payload = client.get(url).json()
            except Exception as exc:
                if progress:
                    progress(sector, 0, f"failed: {exc}")
                continue

            if not isinstance(payload, list):
                # A dict here is the rate-limit message, not data.
                message = payload.get("message") if isinstance(payload, dict) else "unexpected"
                if progress:
                    progress(sector, 0, f"skipped: {message}")
                continue

            added = 0
            for record in payload:
                if not isinstance(record, dict):
                    continue
                label = normalize_sector(record.get("activity")) or sector
                key = index_key(str(record.get("victim") or ""))
                if usable_key(key) and key not in index.entries:
                    index.entries[key] = label
                    added += 1
                host = str(record.get("domain") or "").strip().lower()
                host = host.removeprefix("www.").split("/")[0]
                if host and "." in host and host not in index.domains:
                    index.domains[host] = label

            seen_sectors.add(sector)
            if progress:
                progress(sector, added, f"{len(payload)} record(s)")

    index.sectors = sorted(seen_sectors)
    index.built_at = utcnow().isoformat()
    return index
