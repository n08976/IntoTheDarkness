"""Investigations: a case file that collects findings, notes and evidence."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import UTC
from pathlib import Path

from ..config import Settings, get_settings
from ..models import utcnow
from ..storage import Repository
from ..storage.db import CaseRow


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:120] or "case"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CaseManager:
    """Create cases, attach evidence, and export a readable report.

    Files attached as evidence are copied into ``data/cases/<slug>/`` and hashed,
    so the report can state what was collected and that it has not changed since.
    """

    def __init__(self, repo: Repository, settings: Settings | None = None) -> None:
        self.repo = repo
        self.settings = settings or get_settings()

    def case_dir(self, slug: str) -> Path:
        path = self.settings.data_dir / "cases" / slug
        path.mkdir(parents=True, exist_ok=True)
        return path

    def open_case(
        self, title: str, slug: str | None = None, tags: list[str] | None = None
    ) -> CaseRow:
        slug = slug or slugify(title)
        existing = self.repo.get_case(slug)
        if existing is not None:
            raise ValueError(f"case {slug!r} already exists")
        case = self.repo.create_case(slug, title, tags or [])
        self.case_dir(slug)
        return case

    def _require(self, slug: str) -> CaseRow:
        case = self.repo.get_case(slug)
        if case is None:
            raise KeyError(f"no case named {slug!r}")
        return case

    def add_note(self, slug: str, note: str) -> None:
        case = self._require(slug)
        self.repo.add_evidence(case.id, "note", body=note)

    def add_link(self, slug: str, url: str, note: str = "") -> None:
        case = self._require(slug)
        self.repo.add_evidence(case.id, "link", body=note, payload={"url": url})

    def add_file(self, slug: str, path: Path, note: str = "") -> Path:
        case = self._require(slug)
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise FileNotFoundError(f"{source} is not a file")

        digest = sha256_file(source)
        destination = self.case_dir(slug) / f"{digest[:12]}-{source.name}"
        if not destination.exists():
            shutil.copy2(source, destination)

        self.repo.add_evidence(
            case.id,
            "file",
            body=note,
            payload={
                "original_path": str(source),
                "stored_path": str(destination),
                "bytes": destination.stat().st_size,
            },
            sha256=digest,
        )
        return destination

    def attach_findings(self, slug: str, finding_ids: list[int]) -> int:
        case = self._require(slug)
        return self.repo.attach_findings_to_case(finding_ids, case.id)

    def close_case(self, slug: str, summary: str = "") -> CaseRow | None:
        return self.repo.update_case(slug, status="closed", summary=summary or None)

    # ------------------------------------------------------------------- reporting

    def timeline(self, slug: str) -> list[dict]:
        """Findings and evidence interleaved in chronological order."""
        case = self._require(slug)
        events: list[dict] = []

        for finding in self.repo.case_findings(case.id):
            events.append(
                {
                    "at": _aware(finding.created_at),
                    "type": "finding",
                    "label": f"[{finding.severity}] {finding.kind} — {finding.target}",
                    "body": finding.message,
                    "payload": finding.payload,
                }
            )

        for evidence in self.repo.case_evidence(case.id):
            label = evidence.kind
            if evidence.kind == "link":
                label = f"link — {evidence.payload.get('url', '')}"
            elif evidence.kind == "file":
                label = f"file — {Path(evidence.payload.get('stored_path', '')).name}"
            events.append(
                {
                    "at": _aware(evidence.created_at),
                    "type": evidence.kind,
                    "label": label,
                    "body": evidence.body,
                    "payload": evidence.payload,
                    "sha256": evidence.sha256,
                }
            )

        return sorted(events, key=lambda e: e["at"])

    def export_markdown(self, slug: str, out_path: Path | None = None) -> Path:
        case = self._require(slug)
        events = self.timeline(slug)

        lines = [
            f"# {case.title}",
            "",
            f"- **Case:** `{case.slug}`",
            f"- **Status:** {case.status}",
            f"- **Opened:** {_aware(case.created_at):%Y-%m-%d %H:%M UTC}",
            f"- **Updated:** {_aware(case.updated_at):%Y-%m-%d %H:%M UTC}",
        ]
        if case.tags:
            lines.append(f"- **Tags:** {', '.join(case.tags)}")
        lines += ["", f"**{len(events)} event(s) on the timeline.**", ""]

        if case.summary:
            lines += ["## Summary", "", case.summary, ""]

        lines += ["## Timeline", ""]
        for event in events:
            lines.append(f"### {event['at']:%Y-%m-%d %H:%M UTC} — {event['label']}")
            lines.append("")
            if event.get("body"):
                lines += [event["body"], ""]
            if event.get("sha256"):
                lines += [f"`sha256:{event['sha256']}`", ""]
            payload = event.get("payload")
            if payload and event["type"] == "finding":
                item = (payload or {}).get("item") or {}
                if item.get("url"):
                    lines += [f"<{item['url']}>", ""]
        lines += [
            "---",
            "",
            f"*Generated by IntoTheDarkness on {utcnow():%Y-%m-%d %H:%M UTC}.*",
            "",
        ]

        out_path = out_path or (self.case_dir(slug) / f"{slug}-report.md")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return out_path

    def export_json(self, slug: str, out_path: Path | None = None) -> Path:
        case = self._require(slug)
        payload = {
            "slug": case.slug,
            "title": case.title,
            "status": case.status,
            "summary": case.summary,
            "tags": case.tags,
            "created_at": _aware(case.created_at).isoformat(),
            "timeline": [
                {**e, "at": e["at"].isoformat()} for e in self.timeline(slug)
            ],
        }
        out_path = out_path or (self.case_dir(slug) / f"{slug}.json")
        out_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        return out_path


def _aware(value):
    """SQLite returns naive datetimes; treat them as the UTC we wrote."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value
