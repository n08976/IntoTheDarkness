"""The run loop: scrape → diff → rules → dedupe → notify → record."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from .alerting.rules import RuleSet
from .config import Settings, get_settings
from .enrich import SectorClassifier, normalize_sector
from .models import Finding, FindingKind, Item, Severity, Target
from .notify import Message, get_notifier, render_html, render_subject, render_text
from .scrapers import Fetcher, get_scraper
from .storage import Repository, SnapshotStore, get_db

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    findings: list[Finding] = field(default_factory=list)
    items_scraped: int = 0
    targets_run: int = 0
    targets_skipped: int = 0
    errors: dict[str, str] = field(default_factory=dict)
    suppressed: int = 0
    notified: dict[str, int] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.targets_run} target(s)",
            f"{self.items_scraped} item(s)",
            f"{len(self.findings)} finding(s)",
        ]
        if self.suppressed:
            parts.append(f"{self.suppressed} suppressed")
        if self.targets_skipped:
            parts.append(f"{self.targets_skipped} not due")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


class Pipeline:
    def __init__(
        self,
        settings: Settings | None = None,
        repo: Repository | None = None,
        rules: RuleSet | None = None,
        classifier: SectorClassifier | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or Repository(get_db(self.settings))
        self.rules = rules or RuleSet()
        self.classifier = classifier or SectorClassifier()
        self.snapshots = SnapshotStore(self.settings)

    # ------------------------------------------------------------------ scheduling

    def is_due(self, target: Target, force: bool = False) -> bool:
        if force or target.interval_minutes <= 0:
            return True
        last = self.repo.last_run(target.name)
        if last is None:
            return True
        started = last.started_at
        if started.tzinfo is None:
            started = started.replace(tzinfo=UTC)
        return datetime.now(UTC) - started >= timedelta(minutes=target.interval_minutes)

    # ------------------------------------------------------------------------ scrape

    def scrape_target(self, target: Target, fetcher: Fetcher) -> list[Item]:
        scraper = get_scraper(target.scraper, fetcher)
        # Scrapers that can label sectors are handed the classifier; the rest
        # ignore it.
        scraper.classifier = self.classifier
        items = scraper.scrape(target)
        items = [self._enrich(target, item) for item in items]
        log.debug("target %s produced %d item(s)", target.name, len(items))
        return items

    def _enrich(self, target: Target, item: Item) -> Item:
        """Bound the stored body and make sure every item carries a sector."""
        item = item.truncated(self.settings.max_item_text)

        if target.sector:
            item.fields["sector"] = target.sector
            return item

        # A source that states the industry is better evidence than our keyword
        # guess, so normalise its label onto our vocabulary and keep it.
        supplied = normalize_sector(item.fields.get("sector"))
        if supplied:
            item.fields["sector"] = supplied
            return item

        # Anything else — absent, "unknown", or an upstream label we do not
        # recognise ("Not Found", "Other") — is replaced rather than kept, so a
        # foreign vocabulary never leaks into sector filtering.
        item.fields["sector"] = self.classifier.classify(
            item.title, item.text, use_context=self.settings.sector_use_context
        )
        return item

    def content_mode(self, target: Target) -> str:
        return target.content_mode or self.settings.content_mode

    def run_target(
        self, target: Target, fetcher: Fetcher, force: bool = False
    ) -> tuple[list[Finding], int]:
        """Scrape one target; return its findings and how many items it saw.

        Findings come back already stamped with the target's severity and
        channels, which rules may then raise or extend.
        """
        run_id = self.repo.start_run(target.name)
        try:
            items = self.scrape_target(target, fetcher)
        except Exception as exc:
            log.warning("target %s failed: %s", target.name, exc)
            self.repo.finish_run(run_id, ok=False, error=str(exc))
            if FindingKind.ERROR in target.watch:
                return (
                    [
                        Finding(
                            kind=FindingKind.ERROR,
                            target=target.name,
                            severity=Severity.MEDIUM,
                            message=f"scrape failed: {exc}",
                            channels=list(target.channels),
                        )
                    ],
                    0,
                )
            raise

        findings = self.repo.diff_and_record(
            target.name, items, target.watch, report_baseline=target.report_baseline
        )
        for finding in findings:
            finding.severity = target.severity
            finding.channels = list(target.channels)

        self.repo.finish_run(run_id, items=len(items), findings=len(findings))
        return findings, len(items)

    # --------------------------------------------------------------------- full run

    def run(
        self,
        targets: Sequence[Target],
        force: bool = False,
        dry_run: bool = False,
        notify: bool = True,
    ) -> RunReport:
        report = RunReport()
        tags_by_target = {t.name: t.tags for t in targets}

        with Fetcher(self.settings) as fetcher:
            for target in targets:
                if not target.enabled:
                    report.targets_skipped += 1
                    continue
                if not self.is_due(target, force):
                    report.targets_skipped += 1
                    log.debug("target %s not due yet", target.name)
                    continue

                report.targets_run += 1
                try:
                    findings, item_count = self.run_target(target, fetcher, force)
                except Exception as exc:
                    report.errors[target.name] = str(exc)
                    continue

                report.findings.extend(findings)
                report.items_scraped += item_count

        report.findings = self.rules.apply(report.findings, tags_by_target)

        if not report.findings:
            return report

        if not dry_run:
            self.repo.save_findings(report.findings)

        if notify:
            deliverable = (
                report.findings if dry_run else self._drop_recently_alerted(report)
            )
            if deliverable:
                self._dispatch(deliverable, report, dry_run=dry_run)

        return report

    # ---------------------------------------------------------------- notification

    def _drop_recently_alerted(self, report: RunReport) -> list[Finding]:
        keep: list[Finding] = []
        for finding in report.findings:
            if self.repo.recently_alerted(
                finding.dedupe_key(), self.settings.alert_cooldown_minutes
            ):
                report.suppressed += 1
                continue
            keep.append(finding)
        return keep

    def _routes(self, findings: Sequence[Finding]) -> dict[str, list[Finding]]:
        """Group findings by the channel each should go to."""
        routes: dict[str, list[Finding]] = defaultdict(list)
        for finding in findings:
            channels = finding.channels or ["console"]
            for channel in channels:
                routes[channel].append(finding)
        return dict(routes)

    def _dispatch(
        self, findings: Sequence[Finding], report: RunReport, dry_run: bool = False
    ) -> None:
        for channel, group in self._routes(findings).items():
            if dry_run and channel != "console":
                channel = "console"

            message = Message(
                subject=render_subject(group),
                text=render_text(group),
                html=render_html(group),
                findings=group,
            )

            try:
                notifier = get_notifier(channel, self.settings)
                ok, why = notifier.available()
                if not ok:
                    raise RuntimeError(why)
                notifier.send(message)
            except Exception as exc:
                log.error("channel %s failed: %s", channel, exc)
                report.errors[f"notify:{channel}"] = str(exc)
                if not dry_run:
                    for finding in group:
                        self.repo.record_alert(
                            finding.dedupe_key(), channel, ok=False, error=str(exc)
                        )
                continue

            report.notified[channel] = report.notified.get(channel, 0) + len(group)
            if not dry_run:
                for finding in group:
                    self.repo.record_alert(finding.dedupe_key(), channel, ok=True)
