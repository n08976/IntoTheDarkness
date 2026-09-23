"""The run loop: scrape → diff → rules → dedupe → notify → record."""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlparse

from . import watchlist
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
from .notify.links import merge_links
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
    watchlist: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def summary(self) -> str:
        parts = [
            f"{self.targets_run} target(s)",
            f"{self.items_scraped} item(s)",
            f"{len(self.findings)} finding(s)",
        ]
        if self.watchlist:
            parts.append(f"{len(self.watchlist)} WATCHLIST")
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
        digest: bool = False,
        watchlist_only: bool = False,
    ) -> RunReport:
        """One sweep.

        ``watchlist_only`` scrapes everything but persists nothing and alerts
        only on watchlist matches. It runs between the scheduled reports, so
        a vendor is noticed within the hour; leaving observations unwritten
        means the next scheduled sweep still sees everything as new and
        reports it as it would have anyway.
        """
        report = RunReport()
        tags_by_target = {t.name: t.tags for t in targets}
        # Where each target lives, for entries stored before it had an address.
        self._floors = {t.name: t.base_url for t in targets if t.base_url}
        self._tags = tags_by_target

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
                    findings, item_count = self.run_target(
                        target, fetcher, force, dry_run or watchlist_only
                    )
                except Exception as exc:
                    report.errors[target.name] = str(exc)
                    continue

                report.findings.extend(findings)
                report.items_scraped += item_count

        # Watchlist matches bypass the sector rules: a vendor on any leak site,
        # in any sector, is a third-party exposure. They are found before the
        # rules run, because default_action: ignore would drop them.
        urgent = self._watchlist_matches(report.findings, tags_by_target)
        report.watchlist = urgent
        if watchlist_only:
            report.findings = list(urgent)
        else:
            rest = [f for f in report.findings if f not in urgent]
            report.findings = self.rules.apply(rest, tags_by_target) + list(urgent)

        if not report.findings:
            if digest and notify and not dry_run:
                self._send_daily_digest(targets, report)
            return report

        if not dry_run and not watchlist_only:
            self.repo.save_findings(report.findings)

        if notify:
            deliverable = (
                report.findings if dry_run else self._drop_recently_alerted(report)
            )
            hot = [f for f in deliverable if f in urgent]
            if hot:
                self._send_urgent(hot, report, dry_run=dry_run)
            regular = [f for f in deliverable if f not in urgent]
            if regular:
                self._dispatch(regular, report, dry_run=dry_run, preview=preview)
            elif digest and not dry_run:
                # Everything new was inside its cooldown. The daily report still
                # goes, or a quiet morning is indistinguishable from a dead cron.
                self._send_daily_digest(targets, report)

        return report

    # ------------------------------------------------------------------ watchlist

    def _vendors(self) -> list[watchlist.Vendor]:
        if not hasattr(self, "_vendor_cache"):
            s = self.settings

            def fetch(url: str) -> str:
                with Fetcher(s) as f:
                    return f.request("GET", url, {}, None, None, network="direct").text

            self._vendor_cache = watchlist.refresh(s.watchlist_file, s.watchlist_url, fetch)
        return self._vendor_cache

    def _watchlist_matches(
        self, findings: Sequence[Finding], tags_by_target: dict[str, list[str]]
    ) -> list[Finding]:
        vendors = self._vendors()
        if not vendors:
            return []
        skip = set(self.settings.watchlist_skip_tags)
        eligible = [
            f for f in findings
            if f.kind in (FindingKind.NEW, FindingKind.CHANGED, FindingKind.BASELINE)
            and not (skip & set(tags_by_target.get(f.target, [])))
        ]
        hits = watchlist.find_matches(vendors, eligible)
        matched: list[Finding] = []
        for i, vendor in hits.items():
            f = eligible[i]
            f.rule = f"watchlist:{vendor.name}"
            f.severity = Severity.CRITICAL
            f.channels = list(self.settings.watchlist_channels)
            if f.item is not None:
                f.item.fields["watchlist"] = vendor.name
            matched.append(f)
        return matched

    def _flag_watchlist(self, entries: Sequence[Finding]) -> None:
        vendors = self._vendors()
        if not vendors:
            return
        skip = set(self.settings.watchlist_skip_tags)
        tags = getattr(self, "_tags", {})
        for f in entries:
            if f.item is None or f.item.fields.get("watchlist"):
                continue
            if skip & set(tags.get(f.target, [])):
                continue
            for vendor in vendors:
                if vendor.matches(f.item.title):
                    f.item.fields["watchlist"] = vendor.name
                    break

    def _add_observed_vendor_victims(self, entries: list[Finding]) -> list[Finding]:
        """Vendor listings the rules never reported, found in raw observations."""
        vendors = self._vendors()
        if not vendors:
            return entries
        skip = set(self.settings.watchlist_skip_tags)
        tags = getattr(self, "_tags", {})
        skip_targets = {t for t, tg in tags.items() if skip & set(tg)}
        floors = getattr(self, "_floors", None) or {}
        known = {f.item.title.strip().lower() for f in entries if f.item}
        extra: dict[str, Finding] = {}
        observed = self.repo.observations_since(self.settings.digest_days, skip_targets)
        for f in sorted(observed, key=lambda x: x.created_at):
            if f.item is None:
                continue
            key = f.item.title.strip().lower()
            if key in known:
                continue
            vendor = next((v for v in vendors if v.matches(f.item.title)), None)
            if vendor is None:
                continue
            f.item.fields["watchlist"] = vendor.name
            if not f.item.url and floors.get(f.target):
                f.item.url = floors[f.target]
            if key in extra:
                merge_links(extra[key], f)
            else:
                extra[key] = f
        return list(extra.values()) + entries

    def _send_urgent(
        self, findings: Sequence[Finding], report: RunReport, dry_run: bool = False
    ) -> None:
        """One mail per channel, sent now, naming the vendors in the subject."""
        vendors = sorted({str(f.item.fields.get("watchlist")) for f in findings if f.item})
        subject = f"[URGENT] Vendor on leak site: {', '.join(vendors)}"
        header = (
            "PRIORITY WATCHLIST MATCH\n"
            f"{len(findings)} listing(s) name a vendor on your watchlist. "
            "Sent immediately, outside the scheduled reports.\n\n"
        )
        message = Message(
            subject=subject,
            text=header + render_text(findings),
            html=f"<p><strong>Priority watchlist match.</strong> {len(findings)} listing(s) "
                 f"name a vendor on your watchlist.</p>" + render_html(findings),
            findings=findings,
        )
        for channel in self.settings.watchlist_channels:
            if dry_run and channel not in ("console", "preview"):
                channel = "console"
            try:
                notifier = get_notifier(channel, self.settings)
                ok, why = notifier.available()
                if not ok:
                    raise RuntimeError(why)
                notifier.send(message)
            except Exception as exc:
                log.error("urgent channel %s failed: %s", channel, exc)
                report.errors[f"notify:{channel}"] = str(exc)
                continue
            report.notified[channel] = report.notified.get(channel, 0) + len(findings)
            if not dry_run:
                for f in findings:
                    self.repo.record_alert(f.dedupe_key(), channel, ok=True)

    # ---------------------------------------------------------------- notification

    def _drop_recently_alerted(self, report: RunReport) -> list[Finding]:
        keep: list[Finding] = []
        for finding in report.findings:
            cooldown = (
                self.settings.watchlist_cooldown_minutes
                if (finding.rule or "").startswith("watchlist:")
                else self.settings.alert_cooldown_minutes
            )
            if self.repo.recently_alerted(finding.dedupe_key(), cooldown):
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
        entries = self.repo.discoveries_since(
            self.settings.digest_days, floors=getattr(self, "_floors", None)
        )
        # Entries recorded before the watchlist existed, or before a vendor was
        # added to it, still belong at the top of the report -- including ones
        # the rules never saved as findings, which exist only as observations.
        self._flag_watchlist(entries)
        entries = self._add_observed_vendor_victims(entries)
        new_keys = {f.item.key for f in group if f.item}
        # A finding saved moments ago is already in the window; anything the
        # window missed still belongs in the report, so union rather than trust.
        known = {f.item.key for f in entries if f.item}
        entries = list(entries) + [f for f in group if f.item and f.item.key not in known]

        days = self.settings.digest_days
        if new_keys:
            top = max((f.severity for f in group), key=lambda sev: sev.rank)
            subject = (
                f"[{top.value.upper()}] IntoTheDarkness: {len(new_keys)} new — "
                f"{len(entries)} in the last {days} days"
            )
        else:
            subject = (
                f"IntoTheDarkness daily: no new entries — {len(entries)} in the last {days} days"
            )

        s = self.settings
        text = render_digest_text(
            entries, new_keys, status=status, window_days=s.digest_days,
            priority=s.priority_sectors, carry=s.digest_sectors,
        )
        html = render_digest_html(
            entries, new_keys, status=status, window_days=s.digest_days,
            priority=s.priority_sectors, carry=s.digest_sectors,
        )
        return Message(subject=subject, text=text, html=html, findings=group)

    def _send_daily_digest(self, targets: Sequence[Target], report: RunReport) -> None:
        """Send the full report to every inbox channel even though nothing is new.

        Once a day the report goes regardless, as proof of life. Without it a
        quiet morning and a cron that stopped firing both arrive as an empty
        inbox, and the reader cannot tell which without a shell on the box.
        Console channels are skipped: nobody is watching them at 6am.
        """
        channels = sorted({c for t in targets if t.enabled for c in t.channels})
        for channel in channels:
            try:
                notifier = get_notifier(channel, self.settings)
                if not notifier.wants_digest:
                    continue
                ok, why = notifier.available()
                if not ok:
                    raise RuntimeError(why)
                notifier.send(self._message_for(notifier, [], report))
                report.notified.setdefault(channel, 0)
                log.info("daily digest sent to %s (nothing new)", channel)
            except Exception as exc:
                log.error("daily digest to %s failed: %s", channel, exc)
                report.errors[f"notify:{channel}"] = str(exc)

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
