"""The run loop: scrape → diff → rules → dedupe → notify → record."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

from .alerting.rules import RuleSet
from .config import Settings, get_settings
from .enrich import SectorClassifier, SectorIndex
from .models import Finding, FindingKind, Item, Severity, Target
from .notify import (
    Message,
    Notifier,
    get_notifier,
    render_digest_html,
    render_digest_text,
    render_html,
    render_subject,
    render_text,
)
from .scrapers import Fetcher, get_scraper
from .storage import Repository, SnapshotStore, get_db

log = logging.getLogger(__name__)


@dataclass
class RunReport:
    findings: list[Finding] = field(default_factory=list)
    items_scraped: int = 0
    targets_run: int = 0
    # Disabled and not-yet-due are different states and were reported as one,
    # so a config mistake looked identical to normal interval gating.
    targets_disabled: int = 0
    targets_not_due: int = 0
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
        if self.targets_not_due:
            parts.append(f"{self.targets_not_due} not due")
        if self.targets_disabled:
            parts.append(f"{self.targets_disabled} disabled")
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
        sector_index: SectorIndex | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.repo = repo or Repository(get_db(self.settings))
        self.rules = rules or RuleSet()
        self.classifier = classifier or SectorClassifier()
        # Loaded once per run: an authoritative sector lookup, when one has been
        # built. Absent, resolution falls back to name and domain matching.
        self.sector_index = (
            sector_index if sector_index is not None else SectorIndex.load(self.settings)
        )
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
        self._normalise_links(target, item)

        # Precedence runs by strength of evidence, and the provenance is kept
        # alongside the label so a routing rule can require a stated fact rather
        # than a guess about a company's name.
        result = self.classifier.resolve(
            name=item.title,
            upstream=item.fields.get("sector"),
            # "website" is what most feeds and leak sites call this; falling
            # back to it means a target that maps only that field still gets
            # domain-based classification instead of silently getting none.
            domain=str(item.fields.get("domain") or item.fields.get("website") or ""),
            target_sector=target.sector,
            context=item.text,
            use_context=self.settings.sector_use_context,
            index=self.sector_index if len(self.sector_index) else None,
        )
        item.fields["sector"] = result.sector
        item.fields["sector_source"] = result.source
        return item

    def _normalise_links(self, target: Target, item: Item) -> None:
        """Make both links usable in a report.

        ``item.url`` becomes the leak-site link for this victim, and
        ``fields["website"]`` the victim's own site, each absolute. Sources
        return these in several shapes: a bare domain, a relative path, or a
        full URL.
        """
        if item.url and target.base_url and not urlparse(item.url).scheme:
            item.url = urljoin(target.base_url, item.url)
        if not item.url and target.base_url:
            item.url = target.base_url

        website = str(item.fields.get("website") or "").strip()
        if website:
            if not urlparse(website).scheme:
                website = f"https://{website.lstrip('/')}"
            item.fields["website"] = website
        else:
            item.fields.pop("website", None)

    def content_mode(self, target: Target) -> str:
        return target.content_mode or self.settings.content_mode

    def run_target(
        self, target: Target, fetcher: Fetcher, force: bool = False, dry_run: bool = False
    ) -> tuple[list[Finding], int]:
        """Scrape one target; return its findings and how many items it saw.

        Findings come back already stamped with the target's severity and
        channels, which rules may then raise or extend.
        """
        # A dry run records nothing at all: no run row, so it does not consume
        # the target's interval, and no observations, so it cannot quietly fold
        # today's new victims into the baseline.
        run_id = None if dry_run else self.repo.start_run(target.name)
        try:
            items = self.scrape_target(target, fetcher)
        except Exception as exc:
            log.warning("target %s failed: %s", target.name, exc)
            if run_id is not None:
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
            target.name,
            items,
            target.watch,
            report_baseline=target.report_baseline,
            persist=not dry_run,
        )
        for finding in findings:
            finding.severity = target.severity
            finding.channels = list(target.channels)

        if run_id is not None:
            self.repo.finish_run(run_id, items=len(items), findings=len(findings))
        return findings, len(items)

    # --------------------------------------------------------------------- full run

    def run(
        self,
        targets: Sequence[Target],
        force: bool = False,
        dry_run: bool = False,
        notify: bool = True,
        preview: bool = False,
    ) -> RunReport:
        report = RunReport()
        tags_by_target = {t.name: t.tags for t in targets}

        with Fetcher(self.settings) as fetcher:
            for target in targets:
                if not target.enabled:
                    report.targets_disabled += 1
                    continue
                if not self.is_due(target, force):
                    report.targets_not_due += 1
                    log.debug("target %s not due yet", target.name)
                    continue

                report.targets_run += 1
                try:
                    findings, item_count = self.run_target(target, fetcher, force, dry_run)
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
                self._dispatch(deliverable, report, dry_run=dry_run, preview=preview)

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

    def _message_for(
        self, notifier: Notifier, group: Sequence[Finding], report: RunReport
    ) -> Message:
        """Render for one channel.

        An inbox gets the full report: what is new since the last one, then the
        running list behind it, so a single mail answers both "what just
        happened" and "what has been happening". The console gets the run in
        front of you and nothing else.
        """
        if not notifier.wants_digest:
            return Message(
                subject=render_subject(group),
                text=render_text(group),
                html=render_html(group),
                findings=group,
            )

        status = self._status_line(report)
        entries = self.repo.discoveries_since(self.settings.digest_days)
        new_keys = {f.item.key for f in group if f.item}
        # A finding saved moments ago is already in the window; anything the
        # window missed still belongs in the report, so union rather than trust.
        known = {f.item.key for f in entries if f.item}
        entries = list(entries) + [f for f in group if f.item and f.item.key not in known]

        return Message(
            subject=render_subject(group, new_count=len(new_keys)),
            text=render_digest_text(
                entries, new_keys, status=status, window_days=self.settings.digest_days
            ),
            html=render_digest_html(
                entries, new_keys, status=status, window_days=self.settings.digest_days
            ),
            findings=group,
        )

    def _status_line(self, report: RunReport) -> str:
        """What the sweep actually managed, so an empty list cannot be misread.

        Without this, "no new entries" means both "nothing was posted" and "we
        could not reach anything", and those demand opposite responses.
        """
        parts = [f"{report.targets_run} target(s) swept", f"{report.items_scraped} item(s) read"]
        scrape_errors = {k: v for k, v in report.errors.items() if not k.startswith("notify:")}
        if scrape_errors:
            parts.append(f"{len(scrape_errors)} FAILED: {', '.join(sorted(scrape_errors))}")
        return " · ".join(parts)

    def _dispatch(
        self,
        findings: Sequence[Finding],
        report: RunReport,
        dry_run: bool = False,
        preview: bool = False,
    ) -> None:
        routes = self._routes(findings)
        if preview:
            # Rules can add channels after a target's own list, so the override
            # has to happen here — at dispatch — or a rule silently re-adds
            # email to something the user asked only to preview.
            routes = {"preview": list(findings)}

        for channel, group in routes.items():
            if dry_run and channel not in ("console", "preview"):
                channel = "console"

            try:
                notifier = get_notifier(channel, self.settings)
                ok, why = notifier.available()
                if not ok:
                    raise RuntimeError(why)
                message = self._message_for(notifier, group, report)
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
