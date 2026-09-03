"""Query and mutation helpers over the raw tables."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from ..models import Finding, FindingKind, Item, Severity, utcnow
from .db import AlertRow, CaseRow, Database, EvidenceRow, FindingRow, ObservationRow, RunRow


class Repository:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ---------------------------------------------------------------- observations

    def known_items(self, target: str) -> dict[str, ObservationRow]:
        with self.db.session() as s:
            rows = s.scalars(
                select(ObservationRow).where(ObservationRow.target == target)
            ).all()
            return {row.item_key: row for row in rows}

    def diff_and_record(
        self,
        target: str,
        items: Sequence[Item],
        watch: Sequence[FindingKind],
        report_baseline: bool = False,
    ) -> list[Finding]:
        """Compare a fresh scrape against stored state and persist the new state.

        Returns findings for whichever kinds the target asked to watch.

        A target with no prior observations is normally *seeded* silently: the
        first run should not page you with an entire back catalogue. When
        ``report_baseline`` is set the first run instead emits one BASELINE
        finding per item — the full current picture, once — and every later run
        reports the delta only.
        """
        watched = set(watch)
        findings: list[Finding] = []
        now = utcnow()

        with self.db.session() as s:
            existing = {
                row.item_key: row
                for row in s.scalars(
                    select(ObservationRow).where(ObservationRow.target == target)
                ).all()
            }
            first_run = not existing
            seen_keys: set[str] = set()

            for item in items:
                seen_keys.add(item.key)
                content = item.content_hash()
                row = existing.get(item.key)

                if row is None:
                    s.add(
                        ObservationRow(
                            target=target,
                            item_key=item.key,
                            content_hash=content,
                            payload=item.model_dump(mode="json"),
                            first_seen=now,
                            last_seen=now,
                        )
                    )
                    if first_run:
                        if report_baseline:
                            findings.append(
                                Finding(
                                    kind=FindingKind.BASELINE, target=target, item=item
                                )
                            )
                    elif FindingKind.NEW in watched:
                        findings.append(
                            Finding(kind=FindingKind.NEW, target=target, item=item)
                        )
                    continue

                if row.content_hash != content:
                    old = dict(row.payload or {})
                    row.content_hash = content
                    row.payload = item.model_dump(mode="json")
                    if FindingKind.CHANGED in watched:
                        findings.append(
                            Finding(
                                kind=FindingKind.CHANGED,
                                target=target,
                                item=item,
                                details={
                                    "before": {
                                        k: old.get(k) for k in ("title", "url", "text")
                                    },
                                    "after": {
                                        "title": item.title,
                                        "url": item.url,
                                        "text": item.text,
                                    },
                                },
                            )
                        )
                row.last_seen = now
                row.missing_since = None

            if FindingKind.REMOVED in watched and not first_run:
                for key, row in existing.items():
                    if key in seen_keys or row.missing_since is not None:
                        continue
                    row.missing_since = now
                    findings.append(
                        Finding(
                            kind=FindingKind.REMOVED,
                            target=target,
                            item=Item.model_validate(row.payload)
                            if row.payload
                            else None,
                            message=f"item {key} no longer present",
                        )
                    )

        return findings

    def forget_target(self, target: str) -> int:
        """Drop stored state so the next run re-seeds from scratch."""
        with self.db.session() as s:
            rows = s.scalars(
                select(ObservationRow).where(ObservationRow.target == target)
            ).all()
            for row in rows:
                s.delete(row)
            return len(rows)

    # -------------------------------------------------------------------- findings

    def save_findings(self, findings: Sequence[Finding]) -> list[int]:
        ids: list[int] = []
        with self.db.session() as s:
            for f in findings:
                row = FindingRow(
                    dedupe_key=f.dedupe_key(),
                    target=f.target,
                    kind=f.kind.value,
                    severity=f.severity.value,
                    rule=f.rule,
                    message=f.message or f.headline(),
                    payload=f.model_dump(mode="json"),
                    created_at=f.created_at,
                )
                s.add(row)
                s.flush()
                ids.append(row.id)
        return ids

    def recent_findings(
        self,
        since: datetime | None = None,
        target: str | None = None,
        min_severity: Severity | None = None,
        limit: int = 100,
    ) -> list[FindingRow]:
        stmt = select(FindingRow).order_by(FindingRow.created_at.desc()).limit(limit)
        if since is not None:
            stmt = stmt.where(FindingRow.created_at >= since)
        if target:
            stmt = stmt.where(FindingRow.target == target)
        with self.db.session() as s:
            rows = list(s.scalars(stmt).all())
        if min_severity is not None:
            rows = [r for r in rows if Severity(r.severity).rank >= min_severity.rank]
        return rows

    def attach_findings_to_case(self, finding_ids: Sequence[int], case_id: int) -> int:
        with self.db.session() as s:
            rows = s.scalars(select(FindingRow).where(FindingRow.id.in_(finding_ids))).all()
            for row in rows:
                row.case_id = case_id
            return len(rows)

    # ---------------------------------------------------------------------- alerts

    def recently_alerted(self, dedupe_key: str, cooldown_minutes: int) -> bool:
        if cooldown_minutes <= 0:
            return False
        cutoff = datetime.now(UTC) - timedelta(minutes=cooldown_minutes)
        with self.db.session() as s:
            row = s.scalars(
                select(AlertRow)
                .where(AlertRow.dedupe_key == dedupe_key, AlertRow.ok.is_(True))
                .order_by(AlertRow.sent_at.desc())
                .limit(1)
            ).first()
        if row is None:
            return False
        sent_at = row.sent_at
        if sent_at.tzinfo is None:  # SQLite hands back naive datetimes
            sent_at = sent_at.replace(tzinfo=UTC)
        return sent_at >= cutoff

    def record_alert(
        self, dedupe_key: str, channel: str, ok: bool = True, error: str | None = None
    ) -> None:
        with self.db.session() as s:
            s.add(AlertRow(dedupe_key=dedupe_key, channel=channel, ok=ok, error=error))

    # ------------------------------------------------------------------------ runs

    def start_run(self, target: str) -> int:
        with self.db.session() as s:
            row = RunRow(target=target)
            s.add(row)
            s.flush()
            return row.id

    def finish_run(
        self,
        run_id: int,
        items: int = 0,
        findings: int = 0,
        ok: bool = True,
        error: str | None = None,
    ) -> None:
        with self.db.session() as s:
            row = s.get(RunRow, run_id)
            if row is None:
                return
            row.finished_at = utcnow()
            row.items = items
            row.findings = findings
            row.ok = ok
            row.error = error

    def last_run(self, target: str) -> RunRow | None:
        with self.db.session() as s:
            return s.scalars(
                select(RunRow)
                .where(RunRow.target == target, RunRow.ok.is_(True))
                .order_by(RunRow.started_at.desc())
                .limit(1)
            ).first()

    # ----------------------------------------------------------------------- cases

    def create_case(self, slug: str, title: str, tags: list[str] | None = None) -> CaseRow:
        with self.db.session() as s:
            row = CaseRow(slug=slug, title=title, tags=tags or [])
            s.add(row)
            s.flush()
            return row

    def get_case(self, slug: str) -> CaseRow | None:
        with self.db.session() as s:
            return s.scalars(select(CaseRow).where(CaseRow.slug == slug)).first()

    def list_cases(self, status: str | None = None) -> list[CaseRow]:
        stmt = select(CaseRow).order_by(CaseRow.updated_at.desc())
        if status:
            stmt = stmt.where(CaseRow.status == status)
        with self.db.session() as s:
            return list(s.scalars(stmt).all())

    def update_case(self, slug: str, **fields) -> CaseRow | None:
        with self.db.session() as s:
            row = s.scalars(select(CaseRow).where(CaseRow.slug == slug)).first()
            if row is None:
                return None
            for k, v in fields.items():
                if v is not None and hasattr(row, k):
                    setattr(row, k, v)
            row.updated_at = utcnow()
            return row

    def add_evidence(
        self,
        case_id: int,
        kind: str,
        body: str = "",
        payload: dict | None = None,
        sha256: str | None = None,
    ) -> EvidenceRow:
        with self.db.session() as s:
            row = EvidenceRow(
                case_id=case_id, kind=kind, body=body, payload=payload or {}, sha256=sha256
            )
            s.add(row)
            s.flush()
            case = s.get(CaseRow, case_id)
            if case is not None:
                case.updated_at = utcnow()
            return row

    def case_evidence(self, case_id: int) -> list[EvidenceRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(EvidenceRow)
                    .where(EvidenceRow.case_id == case_id)
                    .order_by(EvidenceRow.created_at.asc())
                ).all()
            )

    def case_findings(self, case_id: int) -> list[FindingRow]:
        with self.db.session() as s:
            return list(
                s.scalars(
                    select(FindingRow)
                    .where(FindingRow.case_id == case_id)
                    .order_by(FindingRow.created_at.asc())
                ).all()
            )
