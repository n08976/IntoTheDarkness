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

    before = repo.last_run(target().name).id

    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target()], dry_run=True)

    assert len(report.findings) == 1
    assert CapturingNotifier.sent == []            # rerouted to console
    assert repo.recent_findings() == []            # nothing written
    assert "Beta" in capsys.readouterr().out

    # The observations must not have been recorded either. Checking only that
    # no findings were written misses the damage that matters: a dry run that
    # quietly seeds today's items makes them "already seen", so the next real
    # run reports nothing and the new victims are lost for good.
    assert repo.last_run(target().name).id == before   # no run row, interval intact
    report = p.run([target()])
    assert [f.item.key for f in report.findings] == ["b"]
    assert len(CapturingNotifier.sent) == 1


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
    assert report.targets_run == 0
    assert report.targets_disabled == 1 and report.targets_not_due == 0

    FEED[:] = [("a", "Alpha")]
    hourly = target(interval_minutes=60)
    p.run([hourly])
    again = p.run([hourly])
    # Not due is reported separately from disabled: a config mistake must not
    # look like normal interval gating.
    assert again.targets_not_due == 1 and again.targets_disabled == 0
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


def test_upstream_sector_label_beats_the_keyword_guess(settings, repo):
    """An aggregator that states the industry is better evidence than our guess."""
    from intothedarkness.models import Item

    class Supplied(Scraper):
        name = "supplied"

        def scrape(self, target):
            return [
                # Upstream says finance; the name alone would say nothing.
                Item(key="a", target=target.name, title="Paylogix",
                     fields={"sector": "Financial Services"}),
                # Upstream says nothing useful; fall back to the name.
                Item(key="b", target=target.name, title="Mercy Hospital",
                     fields={"sector": "Not Found"}),
                # No label at all; fall back to the name.
                Item(key="c", target=target.name, title="Acme Steel Manufacturing"),
            ]

    SCRAPERS["supplied"] = Supplied
    try:
        p = pipeline(settings, repo)
        items = p.scrape_target(target(scraper="supplied"), None)
        sectors = {i.title: i.sector for i in items}
    finally:
        SCRAPERS.pop("supplied", None)

    assert sectors["Paylogix"] == "finance"                    # normalised, not guessed
    assert sectors["Mercy Hospital"] == "healthcare"           # fell back to the name
    assert sectors["Acme Steel Manufacturing"] == "manufacturing"


def test_target_sector_still_overrides_everything(settings, repo):
    from intothedarkness.models import Item

    class Supplied(Scraper):
        name = "supplied2"

        def scrape(self, target):
            return [Item(key="a", target=target.name, title="Paylogix",
                         fields={"sector": "Financial Services"})]

    SCRAPERS["supplied2"] = Supplied
    try:
        p = pipeline(settings, repo)
        items = p.scrape_target(target(scraper="supplied2", sector="defence"), None)
    finally:
        SCRAPERS.pop("supplied2", None)
    assert items[0].sector == "defence"


def test_preview_overrides_channels_that_rules_added(settings, repo):
    """Regression: --preview set the targets' channels, but a rule re-added
    email afterwards, so a preview-only run still tried to send mail."""
    from intothedarkness.alerting import Rule, RuleSet

    rules = RuleSet(rules=[Rule(name="escalate", channels=["capture"])])
    p = pipeline(settings, repo, rules)

    FEED[:] = [("a", "Alpha")]
    p.run([target(channels=[])])
    FEED[:] = [("a", "Alpha"), ("b", "Beta")]
    report = p.run([target(channels=[])], preview=True)

    assert Capture_sent_count() == 0, "rule-added channel was used despite --preview"
    assert report.notified.get("preview") == 1


def Capture_sent_count() -> int:
    return len(CapturingNotifier.sent)


class InboxNotifier(Notifier):
    """Stands in for email: a channel whose reader is not watching the run."""

    name = "inbox"
    wants_digest = True
    sent: list[Message] = []

    def send(self, message: Message) -> None:
        InboxNotifier.sent.append(message)


def test_inbox_channels_get_the_running_list_and_the_console_does_not(settings, repo):
    # The console is read while the run is in front of you; an inbox has to
    # answer both "what just happened" and "what has been happening".
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox", "capture"])])
        FEED[:] = [("a", "Alpha"), ("b", "Beta")]
        p.run([target(channels=["inbox", "capture"])])

        digest = InboxNotifier.sent[-1].text
        plain = CapturingNotifier.sent[-1].text

        assert "NEW SINCE LAST REPORT" in digest
        assert "DISCOVERED IN THE LAST" in digest
        assert "Beta" in digest
        assert "NEW SINCE LAST REPORT" not in plain   # console keeps the run only
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_the_report_says_what_the_sweep_managed(settings, repo):
    # "No new entries" must not be able to mean "we could not reach anything".
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox"])])
        FEED[:] = [("a", "Alpha"), ("b", "Beta")]
        p.run([target(channels=["inbox"])])
        assert "1 target(s) swept" in InboxNotifier.sent[-1].text
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_daily_digest_goes_to_the_inbox_even_when_nothing_is_new(settings, repo):
    # Proof of life. Without it a quiet morning and a dead cron both arrive
    # as an empty inbox.
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox", "capture"])])          # seeds silently
        InboxNotifier.sent = []
        CapturingNotifier.sent = []

        report = p.run([target(channels=["inbox", "capture"])], digest=True)

        assert report.findings == []
        assert len(InboxNotifier.sent) == 1
        mail = InboxNotifier.sent[0]
        assert "no new entries" in mail.subject
        assert "No new entries since the last report" in mail.text
        assert "1 target(s) swept" in mail.text
        assert CapturingNotifier.sent == []                     # console stays quiet
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_without_the_digest_flag_a_quiet_run_sends_nothing(settings, repo):
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox"])])
        InboxNotifier.sent = []
        p.run([target(channels=["inbox"])])
        assert InboxNotifier.sent == []
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def _with_watchlist(settings, tmp_path, names):
    settings.watchlist_file = tmp_path / "vendors.txt"
    settings.watchlist_file.write_text(names)
    settings.watchlist_url = ""                       # no fetch in tests
    settings.watchlist_channels = ["inbox"]
    return settings


def test_watchlist_match_bypasses_ignore_rules_and_goes_out_urgent(settings, repo, tmp_path):
    # default_action: ignore drops everything that is not healthcare; a vendor
    # on a leak site must get through anyway, at once, marked critical.
    _with_watchlist(settings, tmp_path, "Beckman Coulter\nAbbott\n")
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        rules = RuleSet(default_action="ignore", rules=[])
        p = pipeline(settings, repo, rules)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=[])])
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter, Inc"), ("c", "Textile City")]
        report = p.run([target(channels=[])])

        assert [f.item.title for f in report.watchlist] == ["Beckman Coulter, Inc"]
        assert report.findings[0].severity is Severity.CRITICAL
        assert report.findings[0].rule == "watchlist:Beckman Coulter"
        assert len(InboxNotifier.sent) == 1
        assert InboxNotifier.sent[0].subject == "[URGENT] Vendor on leak site: Beckman Coulter"
        assert "Beckman Coulter" in InboxNotifier.sent[0].text
        assert "Textile City" not in InboxNotifier.sent[0].text     # still ignored
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_watchlist_only_sweep_persists_nothing_and_respects_cooldown(settings, repo, tmp_path):
    _with_watchlist(settings, tmp_path, "Beckman Coulter\n")
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo, RuleSet(default_action="ignore", rules=[]))
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=[])])                                   # seed
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter")]

        p.run([target(channels=[])], watchlist_only=True)
        assert len(InboxNotifier.sent) == 1                            # alerted now
        assert repo.recent_findings() == []                            # nothing saved
        assert repo.known_items("t").keys() == {"a"}                   # "b" not seeded

        again = p.run([target(channels=[])], watchlist_only=True)
        assert len(InboxNotifier.sent) == 1                            # cooldown held
        assert again.suppressed == 1

        # The regular sweep still sees it as new and records it for the report.
        regular = p.run([target(channels=[])])
        assert [f.item.key for f in regular.findings] == ["b"]
        assert len(repo.recent_findings()) == 1
        assert len(InboxNotifier.sent) == 1                            # not mailed twice
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_news_headlines_feed_the_watchlist_only_when_they_describe_an_incident(
    settings, repo, tmp_path
):
    # Beckman Coulter, not Microsoft: the biggest platform names are excluded
    # from headline matching by default because they are also simply news.
    _with_watchlist(settings, tmp_path, "Beckman Coulter\n")
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo, RuleSet(default_action="ignore", rules=[]))
        news = target(name="news-x", tags=["news"], channels=[])
        FEED[:] = [("a", "Alpha")]
        p.run([news])
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter Ships a New Analyzer")]
        report = p.run([news])
        assert report.watchlist == [] and InboxNotifier.sent == []      # no incident: not a match
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter Ships a New Analyzer"),
                   ("c", "Beckman Coulter Confirms Breach of Support Systems")]
        report = p.run([news])
        assert [f.item.key for f in report.watchlist] == ["c"]
        assert InboxNotifier.sent[-1].subject == "[URGENT] Vendor on leak site: Beckman Coulter"
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_report_flags_vendor_victims_recorded_before_the_list_existed(settings, repo, tmp_path):
    # A vendor listed last week and added to the watchlist today still leads
    # the next report.
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        p = pipeline(settings, repo)                        # no watchlist yet
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox"])])
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter, Inc")]
        p.run([target(channels=["inbox"])])                 # recorded as an ordinary finding
        assert "VENDOR VICTIMS" not in InboxNotifier.sent[-1].text

        _with_watchlist(settings, tmp_path, "Beckman Coulter\n")
        p2 = pipeline(settings, repo)
        FEED[:] = [("a", "Alpha"), ("b", "Beckman Coulter, Inc"), ("c", "Gamma")]
        p2.run([target(channels=["inbox"])])
        text = InboxNotifier.sent[-1].text
        assert "VENDOR VICTIMS" in text and "[vendor: Beckman Coulter]" in text
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []


def test_report_includes_vendor_listings_the_rules_never_reported(settings, repo, tmp_path):
    # A manufacturing vendor listed before it was on the watchlist was never a
    # finding (ignored by the rules) -- it exists only as an observation.
    CHANNELS["inbox"] = InboxNotifier
    InboxNotifier.sent = []
    try:
        ignore_all = RuleSet(default_action="ignore", rules=[])
        p = pipeline(settings, repo, ignore_all)
        FEED[:] = [("a", "Alpha")]
        p.run([target(channels=["inbox"])])
        FEED[:] = [("a", "Alpha"), ("b", "Konica Minolta Bulgaria")]
        p.run([target(channels=["inbox"])])                       # observed, ignored, unreported
        assert repo.recent_findings() == []

        _with_watchlist(settings, tmp_path, "Konica Minolta\n")
        p2 = pipeline(settings, repo, RuleSet(rules=[]))            # anything new is reported
        FEED[:] = [("a", "Alpha"), ("b", "Konica Minolta Bulgaria"), ("c", "Gamma")]
        p2.run([target(channels=["inbox"])])
        text = InboxNotifier.sent[-1].text
        assert "VENDOR VICTIMS" in text and "Konica Minolta Bulgaria" in text
        assert text.index("Konica Minolta Bulgaria") < text.index("NEW SINCE LAST REPORT")
    finally:
        CHANNELS.pop("inbox", None)
        InboxNotifier.sent = []
