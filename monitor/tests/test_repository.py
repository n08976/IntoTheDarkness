from __future__ import annotations

from intothedarkness.models import FindingKind, Item, Severity

WATCH_ALL = [FindingKind.NEW, FindingKind.CHANGED, FindingKind.REMOVED]


def items(*specs) -> list[Item]:
    return [Item(key=k, target="t", title=title, url=f"https://e.com/{k}") for k, title in specs]


def test_first_run_seeds_silently(repo):
    findings = repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL)
    assert findings == []
    assert set(repo.known_items("t")) == {"a", "b"}


def test_new_items_after_seeding_are_reported(repo):
    repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    findings = repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL)

    assert [f.kind for f in findings] == [FindingKind.NEW]
    assert findings[0].item.key == "b"


def test_unchanged_items_produce_nothing(repo):
    repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    assert repo.diff_and_record("t", items(("a", "A")), WATCH_ALL) == []


def test_changed_items_carry_before_and_after(repo):
    repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    findings = repo.diff_and_record("t", items(("a", "A prime")), WATCH_ALL)

    assert [f.kind for f in findings] == [FindingKind.CHANGED]
    assert findings[0].details["before"]["title"] == "A"
    assert findings[0].details["after"]["title"] == "A prime"


def test_removed_items_report_once_not_every_run(repo):
    repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL)

    first = repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    assert [f.kind for f in first] == [FindingKind.REMOVED]
    assert first[0].item.key == "b"

    second = repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    assert second == []


def test_returning_item_clears_missing_and_can_go_missing_again(repo):
    repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL)
    repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL)

    again = repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    assert [f.kind for f in again] == [FindingKind.REMOVED]


def test_watch_list_gates_which_kinds_surface(repo):
    repo.diff_and_record("t", items(("a", "A")), [FindingKind.NEW])
    findings = repo.diff_and_record("t", items(("a", "changed")), [FindingKind.NEW])
    assert findings == []
    # State is still updated even when the kind isn't watched.
    assert repo.known_items("t")["a"].payload["title"] == "changed"


def test_forget_target_resets_to_seeding(repo):
    repo.diff_and_record("t", items(("a", "A")), WATCH_ALL)
    assert repo.forget_target("t") == 1
    assert repo.diff_and_record("t", items(("a", "A"), ("b", "B")), WATCH_ALL) == []


def test_targets_are_isolated_from_each_other(repo):
    repo.diff_and_record("one", items(("a", "A")), WATCH_ALL)
    assert repo.diff_and_record("two", items(("a", "A")), WATCH_ALL) == []
    assert set(repo.known_items("one")) == {"a"}


def test_alert_cooldown(repo):
    assert repo.recently_alerted("k", 60) is False
    repo.record_alert("k", "email")
    assert repo.recently_alerted("k", 60) is True
    assert repo.recently_alerted("k", 0) is False       # cooldown disabled
    assert repo.recently_alerted("other", 60) is False


def test_failed_alerts_do_not_start_a_cooldown(repo):
    repo.record_alert("k", "email", ok=False, error="smtp down")
    assert repo.recently_alerted("k", 60) is False


def test_findings_are_queryable_by_severity_and_target(repo):
    from intothedarkness.models import Finding

    repo.save_findings([
        Finding(kind=FindingKind.NEW, target="a", severity=Severity.LOW, message="low one"),
        Finding(kind=FindingKind.NEW, target="b", severity=Severity.HIGH, message="high one"),
    ])

    assert len(repo.recent_findings()) == 2
    assert len(repo.recent_findings(target="a")) == 1
    assert len(repo.recent_findings(min_severity=Severity.MEDIUM)) == 1


def test_run_bookkeeping(repo):
    run_id = repo.start_run("t")
    repo.finish_run(run_id, items=5, findings=2)
    last = repo.last_run("t")
    assert last is not None and last.items == 5 and last.ok
