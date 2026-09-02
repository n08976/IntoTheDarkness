from __future__ import annotations

from intothedarkness.models import Finding, FindingKind, Item, Severity, stable_hash


def item(**kw) -> Item:
    base = {"key": "k1", "target": "t", "title": "Title", "url": "https://e.com/1"}
    return Item(**{**base, **kw})


def test_stable_hash_is_deterministic_and_field_separated():
    assert stable_hash("a", "b") == stable_hash("a", "b")
    # The separator prevents "ab"+"" colliding with "a"+"b".
    assert stable_hash("ab", "") != stable_hash("a", "b")


def test_content_hash_tracks_content():
    a = item()
    assert a.content_hash() == item().content_hash()
    assert item(title="Other").content_hash() != a.content_hash()
    assert item(fields={"x": 1}).content_hash() != a.content_hash()


def test_severity_ordering():
    assert Severity.CRITICAL.rank > Severity.HIGH.rank > Severity.INFO.rank


def test_dedupe_key_ignores_content_for_new_but_not_changed():
    new_a = Finding(kind=FindingKind.NEW, target="t", item=item())
    new_b = Finding(kind=FindingKind.NEW, target="t", item=item(title="Edited"))
    assert new_a.dedupe_key() == new_b.dedupe_key()

    changed_a = Finding(kind=FindingKind.CHANGED, target="t", item=item())
    changed_b = Finding(kind=FindingKind.CHANGED, target="t", item=item(title="Edited"))
    assert changed_a.dedupe_key() != changed_b.dedupe_key()


def test_summary_truncates():
    long = item(title="x" * 500)
    assert len(long.summary(80)) == 80
    assert long.summary(80).endswith("…")
