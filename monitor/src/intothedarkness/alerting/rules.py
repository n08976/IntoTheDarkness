"""Rules decide which findings matter, how loudly, and to whom."""

from __future__ import annotations

import re
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field

from ..models import Finding, FindingKind, Severity


class Rule(BaseModel):
    """A match-and-label rule applied to every finding.

    All specified conditions must hold. A matching rule may raise the severity,
    add channels, and optionally stop later rules from running (``stop``) or drop
    the finding entirely (``action: ignore``).
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    enabled: bool = True

    # Conditions
    targets: list[str] = Field(default_factory=list)  # exact names or globs
    kinds: list[FindingKind] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    sectors: list[str] = Field(default_factory=list)  # industry labels
    # Restrict to labels of a given provenance, e.g. only accept a sector that
    # the source itself stated rather than one guessed from the company name.
    sector_sources: list[str] = Field(default_factory=list)
    match: str | None = None  # regex over title/url/text
    not_match: str | None = None

    # Effects
    action: str = "alert"  # alert | ignore
    severity: Severity | None = None
    channels: list[str] = Field(default_factory=list)
    stop: bool = False

    def is_catch_all(self) -> bool:
        """Whether this rule matches every finding it is offered.

        A catch-all ``ignore`` placed after a keep-rule silently swallows what
        the keep-rule matched, unless that rule sets ``stop``. Used to warn
        about that ordering rather than leaving it to be discovered in prod.
        """
        return not (self.kinds or self.tags or self.sectors or self.match)

    def matches(self, finding: Finding, target_tags: Sequence[str] = ()) -> bool:
        if not self.enabled:
            return False
        if self.targets and not any(
            _glob(pattern, finding.target) for pattern in self.targets
        ):
            return False
        if self.kinds and finding.kind not in self.kinds:
            return False
        if self.tags and not set(self.tags) & set(target_tags):
            return False
        if self.sectors:
            sector = finding.item.sector if finding.item else None
            if sector is None or sector.lower() not in {s.lower() for s in self.sectors}:
                return False
        if self.sector_sources:
            source = finding.item.sector_source if finding.item else None
            if source is None or source.lower() not in {s.lower() for s in self.sector_sources}:
                return False

        haystack = _haystack(finding)
        if self.match and not re.search(self.match, haystack, re.I | re.S):
            return False
        return not (self.not_match and re.search(self.not_match, haystack, re.I | re.S))


def _glob(pattern: str, value: str) -> bool:
    from fnmatch import fnmatch

    return fnmatch(value, pattern)


def _haystack(finding: Finding) -> str:
    parts = [finding.target, finding.kind.value, finding.message]
    if finding.item:
        parts += [finding.item.title, finding.item.url, finding.item.text]
        parts += [f"{k}={v}" for k, v in finding.item.fields.items()]
    return "\n".join(str(p) for p in parts)


class RuleSet(BaseModel):
    rules: list[Rule] = Field(default_factory=list)
    # "alert" keeps anything no rule dropped; "ignore" keeps only what a rule
    # explicitly matched. The latter expresses "only these" directly, instead of
    # requiring a keep-rule with `stop: true` followed by a catch-all ignore —
    # an ordering that silently drops everything if you forget the `stop`.
    default_action: str = "alert"

    def apply(
        self, findings: Sequence[Finding], target_tags: dict[str, list[str]] | None = None
    ) -> list[Finding]:
        """Return the findings that survive, enriched with severity and channels."""
        tags_by_target = target_tags or {}
        kept: list[Finding] = []

        keep_only = self.default_action == "ignore"

        for finding in findings:
            tags = tags_by_target.get(finding.target, [])
            dropped = False
            matched = False

            for rule in self.rules:
                if not rule.matches(finding, tags):
                    continue
                if rule.action == "ignore":
                    dropped = True
                    break
                matched = True

                if rule.severity is not None and rule.severity.rank > finding.severity.rank:
                    finding.severity = rule.severity
                for channel in rule.channels:
                    if channel not in finding.channels:
                        finding.channels.append(channel)
                finding.rule = rule.name

                if rule.stop:
                    break

            if dropped:
                continue
            if keep_only and not matched:
                continue
            kept.append(finding)

        return kept


def ordering_warnings(rules: Sequence[Rule]) -> list[str]:
    """Flag rule orderings that drop more than their author probably intended."""
    warnings: list[str] = []
    for index, rule in enumerate(rules):
        if not rule.enabled or rule.action != "ignore" or not rule.is_catch_all():
            continue
        swallowed = [
            earlier.name
            for earlier in rules[:index]
            if earlier.enabled and earlier.action != "ignore" and not earlier.stop
        ]
        if swallowed:
            warnings.append(
                f"rule {rule.name!r} ignores everything, so findings matched by "
                f"{', '.join(repr(n) for n in swallowed)} are dropped too — "
                f"add 'stop: true' to those rules to keep them"
            )
    return warnings
