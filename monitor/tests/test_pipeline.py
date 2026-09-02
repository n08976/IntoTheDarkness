from __future__ import annotations

import pytest

from intothedarkness.alerting import Rule, RuleSet
from intothedarkness.models import FindingKind, Item, Severity, Target
from intothedarkness.notify.base import REGISTRY as CHANNELS
from intothedarkness.notify.base import Message, Notifier
from intothedarkness.pipeline import Pipeline
from intothedarkness.scrapers.base import REGISTRY as SCRAPERS
from intothedarkness.scrapers.base import Scraper

# Items the fake scraper will return; tests mutate this between runs.
FEED: list[tuple[str, str]] = []
FAIL = {"boom": False}


class FakeScraper(Scraper):
    name = "fake"

    def scrape(self, target: Target) -> list[Item]:
        if FAIL["boom"]:
            raise RuntimeError("site is down")
        return self.filter(
            target,
            [
                Item(key=k, target=target.name, title=t, url=f"https://e.com/{k}")
                for k, t in FEED
            ],
        )


class CapturingNotifier(Notifier):
    name = "capture"
    sent: list[Message] = []
    fail = False

    def send(self, message: Message) -> None:
        if CapturingNotifier.fail:
            raise RuntimeError("channel exploded")
        CapturingNotifier.sent.append(message)


@pytest.fixture(autouse=True)
def register_fakes():
    SCRAPERS["fake"] = FakeScraper
    CHANNELS["capture"] = CapturingNotifier
    FEED.clear()
    FAIL["boom"] = False
    CapturingNotifier.sent = []
    CapturingNotifier.fail = False
    yield
    SCRAPERS.pop("fake", None)
    CHANNELS.pop("capture", None)


def target(**kw) -> Target:
    base = dict(
        name="t",
        url="https://e.com/",
        scraper="fake",
        interval_minutes=0,
        watch=[FindingKind.NEW, FindingKind.CHANGED],
        channels=["capture"],
    )
    return Target(**{**base, **kw})


def pipeline(settings, repo, rules=None) -> Pipeline:
    return Pipeline(settings=settings, repo=repo, rules=rules or RuleSet())


def test_first_run_seeds_and_sends_nothing(settings, repo):
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = pipeline(settings, repo).run([target()])

    assert report.findings == []
    assert CapturingNotifier.sent == []
    assert report.targets_run == 1


def test_second_run_detects_and_notifies(settings, repo):
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target()])

    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()])

    assert [f.item.key for f in report.findings] == ["b"]
    assert len(CapturingNotifier.sent) == 1
    assert "Beta" in CapturingNotifier.sent[0].text
    assert report.notified == {"capture": 1}


def test_cooldown_suppresses_a_repeat_of_the_same_finding(settings, repo):
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target()])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    p.run([target()])
    assert len(CapturingNotifier.sent) == 1

    # Forgetting state makes the pipeline re-detect "b" as new; the alert
    # cooldown, not the observation store, must be what stops the second email.
    repo.forget_target("t")
    FEED[:] = [("a", "Alpha")]
    p.run([target()])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()])

    assert len(report.findings) == 1
    assert report.suppressed == 1
    assert len(CapturingNotifier.sent) == 1


def test_dry_run_persists_nothing_and_routes_to_console(settings, repo, capsys):
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target()])

    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()], dry_run=True)

    assert len(report.findings) == 1
    assert CapturingNotifier.sent == []            # rerouted to console
    assert repo.recent_findings() == []            # nothing written
    assert "Beta" in capsys.readouterr().out


def test_no_notify_detects_and_records_but_sends_nothing(settings, repo):
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target()])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()], notify=False)

    assert len(report.findings) == 1
    assert CapturingNotifier.sent == []
    assert len(repo.recent_findings()) == 1


def test_rules_reroute_and_escalate(settings, repo):
    rules = RuleSet(rules=[
        Rule(name="escalate", match="breach", severity=Severity.CRITICAL, channels=["capture"]),
        Rule(name="quiet", match="signup", action="ignore"),
    ])
    p = pipeline(settings, repo, rules)

    FEED[:] = [("a", "Alpha")]
    p.run([target(channels=[])])
    FEED[:] = [("a", "Alpha"), ("b", "data breach found"), ("c", "newsletter signup")]
    report = p.run([target(channels=[])])

    kinds = {f.item.key: f for f in report.findings}
    assert set(kinds) == {"b"}                      # "c" was ignored
    assert kinds["b"].severity is Severity.CRITICAL
    assert len(CapturingNotifier.sent) == 1


def test_scrape_failure_is_recorded_not_raised(settings, repo):
    FAIL["boom"] = True
    report = pipeline(settings, repo).run([target()])

    assert not report.ok
    assert "site is down" in report.errors["t"]
    last = repo.last_run("t")
    assert last is None                             # failed runs aren't "last ok run"


def test_error_kind_turns_a_failure_into_a_finding(settings, repo):
    FAIL["boom"] = True
    report = pipeline(settings, repo).run([target(watch=[FindingKind.NEW, FindingKind.ERROR])])

    assert report.ok
    assert [f.kind for f in report.findings] == [FindingKind.ERROR]
    assert report.findings[0].severity is Severity.MEDIUM
    assert len(CapturingNotifier.sent) == 1


def test_notifier_failure_is_reported_and_leaves_no_cooldown(settings, repo):
    CapturingNotifier.fail = True
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target()])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()])

    assert "notify:capture" in report.errors
    key = report.findings[0].dedupe_key()
    assert repo.recently_alerted(key, 60) is False   # so the next run retries


def test_disabled_and_not_due_targets_are_skipped(settings, repo):
    p = pipeline(settings, repo)
    report = p.run([target(enabled=False)])
    assert report.targets_run == 0 and report.targets_skipped == 1

    FEED[:] = [("a", "Alpha")]
    hourly = target(interval_minutes=60)
    p.run([hourly])
    again = p.run([hourly])
    assert again.targets_skipped == 1
    assert p.run([hourly], force=True).targets_run == 1


def test_findings_route_to_multiple_channels(settings, repo, capsys):
    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    p.run([target(channels=["capture", "console"])])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target(channels=["capture", "console"])])

    assert report.notified == {"capture": 1, "console": 1}
    assert "Beta" in capsys.readouterr().out


def test_report_counts_items_scraped_not_findings(settings, repo):
    FEED[:] = [("a", "Alpha"), ("b", "Beta"), ("c", "Gamma")]
    report = pipeline(settings, repo).run([target()])

    assert report.findings == []          # first run seeds
    assert report.items_scraped == 3      # but three items were still fetched


def test_run_target_returns_findings_and_item_count(settings, repo):
    from intothedarkness.scrapers import Fetcher

    p = pipeline(settings, repo)
    FEED[:] = [("a", "Alpha")]
    with Fetcher(settings) as fetcher:
        findings, count = p.run_target(target(), fetcher)
        assert (findings, count) == ([], 1)

        FEED[:] = [("a", "Alpha"), ("b", "Beta")]
        findings, count = p.run_target(target(), fetcher)
        assert count == 2 and len(findings) == 1
