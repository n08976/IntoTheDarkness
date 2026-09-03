"""Import leak-site addresses from a ransomwatch-format ``groups.json``.

ransomwatch (and its live forks) maintain the thing that is genuinely hard to
keep current: which groups exist and which of their onion mirrors answer today.
It does *not* provide selectors — it uses hand-written per-site Python parsers —
so an imported target starts as whole-page change detection and is upgraded to
victim extraction once its selectors are known.

Format: a list of groups, each with ``name``, ``captcha``, ``javascript_render``
and a list of ``locations`` carrying ``fqdn``, ``version``, ``available`` and
``enabled``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..models import Target
from ..scrapers.dls import identity_key
from ..tor import ONION_V3_RE

log = logging.getLogger(__name__)

DEFAULT_SOURCE = (
    "https://raw.githubusercontent.com/cyberiskvision/dls-monitor/main/groups.json"
)


@dataclass
class Group:
    name: str
    captcha: bool = False
    javascript: bool = False
    has_parser: bool = False
    meta: str = ""
    hosts: list[dict] = field(default_factory=list)

    @property
    def usable_hosts(self) -> list[dict]:
        """v3 mirrors that the source believes are enabled and answering."""
        return [
            h
            for h in self.hosts
            if h.get("enabled")
            and h.get("available")
            and ONION_V3_RE.match(str(h.get("fqdn", "")))
        ]

    @property
    def skip_reason(self) -> str | None:
        """Why this group cannot be scraped by us, if it cannot."""
        if not self.usable_hosts:
            return "no reachable v3 mirror"
        if self.captcha:
            return "captcha-gated"
        if self.javascript:
            return "needs JavaScript rendering"
        return None


@dataclass
class ImportReport:
    groups_seen: int = 0
    hosts_seen: int = 0
    targets: list[dict] = field(default_factory=list)
    skipped: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"{len(self.targets)} target(s) from {self.groups_seen} group(s) / "
            f"{self.hosts_seen} host(s); {len(self.skipped)} skipped"
        )


def parse_groups(data: Any) -> list[Group]:
    if not isinstance(data, list):
        raise ValueError("groups.json must be a list of group objects")
    groups = []
    for entry in data:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        groups.append(
            Group(
                name=str(entry["name"]),
                captcha=bool(entry.get("captcha")),
                javascript=bool(entry.get("javascript_render")),
                has_parser=bool(entry.get("parser")),
                meta=str(entry.get("meta") or ""),
                hosts=[h for h in entry.get("locations", []) if isinstance(h, dict)],
            )
        )
    return groups


def load_groups(path: Path) -> list[Group]:
    return parse_groups(json.loads(Path(path).read_text(encoding="utf-8")))


def build_targets(
    groups: list[Group],
    scraper: str = "page",
    enabled: bool = False,
    interval_minutes: int = 360,
    channels: list[str] | None = None,
    include_unavailable: bool = False,
    only: set[str] | None = None,
) -> ImportReport:
    """Turn groups into target definitions, one per group's best mirror.

    Defaults are deliberately cautious: ``page`` scraping (no selectors needed)
    and ``enabled: false``, so an import never starts hitting 48 hidden services
    on its own. Extra mirrors are recorded in ``notes`` rather than becoming
    separate targets, since they serve the same content and would double-report.
    """
    report = ImportReport()
    channels = channels or ["email"]

    for group in groups:
        report.groups_seen += 1
        report.hosts_seen += len(group.hosts)

        if only and group.name not in only:
            continue

        reason = group.skip_reason
        hosts = group.usable_hosts
        if not hosts and include_unavailable:
            hosts = [
                h
                for h in group.hosts
                if h.get("enabled") and ONION_V3_RE.match(str(h.get("fqdn", "")))
            ]
            reason = None if hosts else reason

        if reason is not None:
            report.skipped[group.name] = reason
            continue

        primary, *mirrors = hosts
        notes = [group.meta] if group.meta else []
        if mirrors:
            notes.append(f"mirrors: {', '.join(h['fqdn'] for h in mirrors)}")
        if group.has_parser:
            notes.append("upstream ships a custom parser; selectors will need tuning")

        target: dict[str, Any] = {
            "name": f"dls-{group.name}",
            "url": f"http://{primary['fqdn']}/",
            "scraper": scraper,
            "network": "tor",
            "enabled": enabled,
            "interval_minutes": interval_minutes,
            "watch": ["new", "removed"] if scraper == "dls" else ["changed"],
            "report_baseline": True,
            "content_mode": "hash",
            "severity": "high",
            "channels": list(channels),
            "tags": ["dls", "ransomware", group.name],
        }
        if scraper == "dls":
            # Placeholders: `itd targets suggest` proposes real ones from the page.
            target["selectors"] = {"item": "TODO", "title": "TODO"}
        if notes:
            target["notes"] = " | ".join(notes)

        report.targets.append(target)

    return report


def to_yaml(targets: list[dict]) -> str:
    import yaml

    header = (
        "# Generated by `itd import ransomwatch`.\n"
        "# Targets are disabled and set to whole-page change detection. To extract\n"
        "# victim names, run `itd targets suggest <name>` against a live mirror,\n"
        "# paste the selectors, set `scraper: dls`, then enable.\n"
    )
    body = yaml.safe_dump(
        {"targets": targets}, sort_keys=False, default_flow_style=False, width=100
    )
    return header + body


def seed_keys_from_posts(path: Path, group: str | None = None) -> list[tuple[str, str]]:
    """Read ``posts.json`` into (identity key, victim name) pairs.

    Used to pre-load a target's observations so the first real run reports only
    genuinely new victims instead of the entire history.
    """
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError("posts.json must be a list of post objects")

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for post in data:
        if not isinstance(post, dict):
            continue
        if group and post.get("group_name") != group:
            continue
        title = str(post.get("post_title") or "").strip()
        if not title:
            continue
        key = identity_key(title)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append((key, title))
    return out


def validate_targets(targets: list[dict]) -> list[str]:
    """Make sure generated entries actually parse as Targets."""
    problems = []
    for entry in targets:
        candidate = {k: v for k, v in entry.items() if k != "selectors"}
        if "selectors" in entry and entry["selectors"].get("item") != "TODO":
            candidate["selectors"] = entry["selectors"]
        try:
            Target.model_validate(candidate)
        except Exception as exc:
            problems.append(f"{entry.get('name')}: {exc}")
    return problems
