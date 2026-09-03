"""Core domain objects: what we watch, what we see, and what we conclude."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


def utcnow() -> datetime:
    return datetime.now(UTC)


def stable_hash(*parts: str) -> str:
    """Short, deterministic identity for an item or an alert."""
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return digest[:16]


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @property
    def rank(self) -> int:
        return _SEVERITY_ORDER[self]


_SEVERITY_ORDER = {
    Severity.INFO: 0,
    Severity.LOW: 1,
    Severity.MEDIUM: 2,
    Severity.HIGH: 3,
    Severity.CRITICAL: 4,
}


class FindingKind(StrEnum):
    NEW = "new"
    REMOVED = "removed"
    CHANGED = "changed"
    MATCH = "match"
    ERROR = "error"
    # Emitted on a target's first run when it asks for a baseline report: the
    # full current state, once, so later runs can be deltas only.
    BASELINE = "baseline"


class Selectors(BaseModel):
    """How to pull items out of a fetched document.

    ``item`` is the repeating container; the rest are read relative to it. When
    ``item`` is omitted the whole document is treated as a single item, which is
    the right shape for "did this page change at all" watching.
    """

    item: str | None = None
    title: str | None = None
    link: str | None = None
    text: str | None = None
    key: str | None = None
    attrs: dict[str, str] = Field(default_factory=dict)


class Target(BaseModel):
    """One thing we watch."""

    model_config = ConfigDict(extra="forbid")

    name: str
    url: str
    scraper: str = "css"
    enabled: bool = True
    interval_minutes: int = 60
    method: str = "GET"
    headers: dict[str, str] = Field(default_factory=dict)
    params: dict[str, str] = Field(default_factory=dict)
    body: str | None = None

    selectors: Selectors = Field(default_factory=Selectors)
    # For the json scraper: dotted path to the list of records, e.g. "data.items".
    json_path: str | None = None
    json_fields: dict[str, str] = Field(default_factory=dict)

    # Post-extraction filtering.
    include: str | None = None  # regex an item must match to be kept
    exclude: str | None = None  # regex that drops an item

    # Which network to fetch over: auto (.onion implies Tor), direct, or tor.
    # An explicit value makes a misrouted target fail loudly instead of leaking
    # a hidden-service lookup onto clearnet DNS.
    network: str = "auto"
    # "hash" keeps only a digest of the body; "store" writes a snapshot to disk.
    # Inherits the global default when unset.
    content_mode: str | None = None
    # Send the whole current state once, on the first run, then deltas only.
    report_baseline: bool = False
    # Optional sector label applied to every item this target produces, used
    # when a target covers a single industry.
    sector: str | None = None

    watch: list[FindingKind] = Field(default_factory=lambda: [FindingKind.NEW])
    severity: Severity = Severity.INFO
    channels: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("target name must not be empty")
        return v

    @field_validator("network")
    @classmethod
    def _known_network(cls, v: str) -> str:
        allowed = {"auto", "direct", "tor"}
        if v not in allowed:
            raise ValueError(f"network must be one of {sorted(allowed)}, got {v!r}")
        return v

    @field_validator("content_mode")
    @classmethod
    def _known_content_mode(cls, v: str | None) -> str | None:
        allowed = {"hash", "store"}
        if v is not None and v not in allowed:
            raise ValueError(f"content_mode must be one of {sorted(allowed)}, got {v!r}")
        return v


class Item(BaseModel):
    """A single extracted record from a target."""

    key: str
    target: str
    title: str = ""
    url: str = ""
    text: str = ""
    fields: dict[str, Any] = Field(default_factory=dict)
    seen_at: datetime = Field(default_factory=utcnow)

    def content_hash(self) -> str:
        payload = "\n".join(
            [self.title, self.url, self.text]
            + [f"{k}={self.fields[k]!r}" for k in sorted(self.fields)]
        )
        return stable_hash(payload)

    def summary(self, width: int = 160) -> str:
        base = self.title or self.text or self.url or self.key
        base = " ".join(base.split())
        return base if len(base) <= width else base[: width - 1] + "…"

    @property
    def sector(self) -> str | None:
        value = self.fields.get("sector")
        return str(value) if value else None

    @property
    def sector_source(self) -> str | None:
        """Where the sector label came from: target, upstream, propagated,
        name, domain or none. Routing on a label is only safe if you can tell
        a stated fact from a guess."""
        value = self.fields.get("sector_source")
        return str(value) if value else None

    def truncated(self, max_text: int) -> Item:
        """A copy with the body bounded, so one huge page cannot bloat storage."""
        if max_text <= 0 or len(self.text) <= max_text:
            return self
        clone = self.model_copy(deep=True)
        clone.text = self.text[:max_text]
        clone.fields = {**self.fields, "truncated_from": len(self.text)}
        return clone


class Finding(BaseModel):
    """Something worth a human's attention."""

    kind: FindingKind
    target: str
    severity: Severity = Severity.INFO
    item: Item | None = None
    message: str = ""
    details: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utcnow)
    rule: str | None = None
    channels: list[str] = Field(default_factory=list)

    def dedupe_key(self) -> str:
        """Identity used to suppress repeat alerts.

        Content is folded in only for CHANGED findings, so a page that keeps
        changing keeps alerting while a stable new item alerts once.
        """
        item_key = self.item.key if self.item else "-"
        content = "-"
        if self.item and self.kind is FindingKind.CHANGED:
            content = self.item.content_hash()
        return stable_hash(self.target, self.kind.value, item_key, content)

    def headline(self) -> str:
        subject = self.item.summary() if self.item else self.message
        return f"[{self.severity.value}] {self.target}: {self.kind.value} — {subject}"
