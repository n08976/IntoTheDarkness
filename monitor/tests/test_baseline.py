"""Stage 1 behaviour: one full baseline report, then deltas only."""

from __future__ import annotations

import pytest

from intothedarkness.models import Finding, FindingKind, Item, Target
from intothedarkness.notify import render_subject, render_text
from intothedarkness.notify.base import REGISTRY as CHANNELS
from intothedarkness.notify.base import Message, Notifier
from intothedarkness.notify.render import is_baseline
from intothedarkness.pipeline import Pipeline
from intothedarkness.scrapers.base import REGISTRY as SCRAPERS
from intothedarkness.scrapers.base import Scraper

VICTIMS: list[tuple[str, str]] = []


class FakeDls(Scraper):
    name = "fake-dls"

    def scrape(self, target: Target) -> list[Item]:
        return [
            Item(key=key, target=target.name, title=title, fields={"company": title})
            for key, title in VICTIMS
        ]


class Capture(Notifier):
    name = "capture"
    sent: list[Message] = []

    def send(self, message: Message) -> None:
        Capture.sent.append(message)


@pytest.fixture(autouse=True)
def fakes():
    SCRAPERS["fake-dls"] = FakeDls
    CHANNELS["capture"] = Capture
    VICTIMS.clear()
    Capture.sent = []
    yield
    SCRAPERS.pop("fake-dls", None)
    CHANNELS.pop("capture", None)


def target(**kw) -> Target:
    base = dict(
        name="leaks",
        url="http://example.onion/",
        scraper="fake-dls",
        interval_minutes=0,
        watch=[FindingKind.NEW, FindingKind.REMOVED],
        report_baseline=True,
        channels=["capture"],
    )
    return Target(**{**base, **kw})


def test_first_run_reports_the_whole_list_once(settings, repo):
    VICTIMS[:] = [("a", "St Mary Hospital"), ("b", "Acme Steel Ltd")]
    report = Pipeline(settings=settings, repo=repo).run([target()])

    assert {f.kind for f in report.findings} == {FindingKind.BASELINE}
    assert len(report.findings) == 2
    assert len(Capture.sent) == 1
    assert "baseline" in Capture.sent[0].subject.lower()


def test_second_run_sends_nothing_when_unchanged(settings, repo):
    p = Pipeline(settings=settings, repo=repo)
    VICTIMS[:] = [("a", "St Mary Hospital")]
    p.run([target()])
    Capture.sent = []

    report = p.run([target()])
    assert report.findings == []
    assert Capture.sent == []


def test_later_runs_report_only_the_delta(settings, repo):
    p = Pipeline(settings=settings, repo=repo)
    VICTIMS[:] = [("a", "St Mary Hospital")]
    p.run([target()])
    Capture.sent = []

    VICTIMS[:] = [("a", "St Mary Hospital"), ("b", "Acme Steel Ltd")]
    report = p.run([target()])

    assert [f.kind for f in report.findings] == [FindingKind.NEW]
    assert report.findings[0].item.title == "Acme Steel Ltd"
    assert "Acme Steel" in Capture.sent[0].text
    assert "St Mary" not in Capture.sent[0].text  # not repeated


def test_without_report_baseline_the_first_run_stays_silent(settings, repo):
    VICTIMS[:] = [("a", "St Mary Hospital")]
    report = Pipeline(settings=settings, repo=repo).run([target(report_baseline=False)])

    assert report.findings == []
    assert Capture.sent == []


def test_removed_victims_are_reported(settings, repo):
    p = Pipeline(settings=settings, repo=repo)
    VICTIMS[:] = [("a", "St Mary Hospital"), ("b", "Acme Steel Ltd")]
    p.run([target()])
    Capture.sent = []

    VICTIMS[:] = [("a", "St Mary Hospital")]
    report = p.run([target()])
    assert [f.kind for f in report.findings] == [FindingKind.REMOVED]


def test_sectors_are_attached_during_the_pipeline(settings, repo):
    VICTIMS[:] = [("a", "St Mary Hospital"), ("b", "Acme Steel Manufacturing")]
    report = Pipeline(settings=settings, repo=repo).run([target()])
    sectors = {f.item.title: f.item.sector for f in report.findings}
    assert sectors == {
        "St Mary Hospital": "healthcare",
        "Acme Steel Manufacturing": "manufacturing",
    }


# ------------------------------------------------------------------- rendering


def victim_finding(name, sector, kind=FindingKind.BASELINE) -> Finding:
    return Finding(
        kind=kind,
        target="leaks",
        item=Item(key=name, target="leaks", title=name, fields={"sector": sector}),
    )


def test_baseline_subject_does_not_read_as_an_alert():
    subject = render_subject([victim_finding("A", "healthcare")])
    assert "baseline" in subject.lower()
    assert "CRITICAL" not in subject and "HIGH" not in subject


def test_baseline_body_leads_with_a_sector_census():
    findings = [
        victim_finding("St Mary Hospital", "healthcare"),
        victim_finding("Northside Clinic", "healthcare"),
        victim_finding("Acme Steel", "manufacturing"),
        victim_finding("Zzyzx Ltd", "unknown"),
    ]
    body = render_text(findings)

    assert "BASELINE — 4 entries" in body
    assert "changes only" in body
    census = body.split("By sector:")[1]
    # Largest sector first, unknown last.
    assert census.index("healthcare") < census.index("manufacturing")
    assert census.index("manufacturing") < census.index("unknown")


def test_a_mixed_report_is_not_treated_as_a_baseline():
    findings = [
        victim_finding("A", "healthcare"),
        victim_finding("B", "finance", kind=FindingKind.NEW),
    ]
    assert not is_baseline(findings)
    assert "baseline" not in render_subject(findings).lower()


def test_delta_lines_carry_the_sector():
    body = render_text([victim_finding("Acme Steel", "manufacturing", FindingKind.NEW)])
    assert "[manufacturing]" in body
