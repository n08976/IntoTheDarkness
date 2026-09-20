"""The date policy: show what the source said, never invent what it did not."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from intothedarkness.models import Finding, FindingKind, Item
from intothedarkness.notify.dates import AS_REPORTED, FIRST_SEEN, PUBLISHED, stamp_for

SEEN = datetime(2026, 9, 12, 17, 5, tzinfo=UTC)


def finding(**fields) -> Finding:
    item = Item(key="k", target="t", title="Acme", fields=fields, seen_at=SEEN)
    return Finding(kind=FindingKind.NEW, target="t", item=item, created_at=SEEN)


# Every format below was taken from the live database, one per source.
@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-01-15T13:48:29.435004+00:00", "2026-01-15 13:48 UTC"),  # ransomware.live
        ("2026-09-03 14:45:56.940420", "2026-09-03 14:45 UTC"),        # ransomlook
        ("2026-08-24", "2026-08-24"),                                  # attacks feed
    ],
)
def test_parseable_source_dates_are_labelled_published(raw, expected):
    stamp = stamp_for(finding(discovered=raw))
    assert (stamp.label, stamp.text) == (PUBLISHED, expected)
    assert stamp.when is not None


def test_a_human_written_date_with_a_year_is_parsed():
    stamp = stamp_for(finding(published="24 August 2026"))   # dragonforce
    assert (stamp.label, stamp.text) == (PUBLISHED, "2026-08-24")


def test_a_date_without_a_year_is_shown_verbatim_and_never_guessed():
    # Everest writes "Sep 1". Inferring the year would let a report claim a
    # breach from an earlier year happened days ago.
    stamp = stamp_for(finding(published="Sep 1"))
    assert (stamp.label, stamp.text) == (AS_REPORTED, "Sep 1")
    assert stamp.when is None


def test_no_source_date_falls_back_to_our_clock_and_says_so():
    # Daixin and Rhysida publish no date. Presenting our observation time as
    # though the group had stated it would be a fabricated provenance.
    stamp = stamp_for(finding())
    assert stamp.label == FIRST_SEEN
    assert stamp.text == "2026-09-12 17:05 UTC"


def test_a_bare_date_does_not_gain_a_fake_midnight():
    assert "00:00" not in stamp_for(finding(discovered="2026-08-24")).text


def test_history_is_ordered_by_the_date_the_reader_sees():
    # Sorting on discovery time while printing the source's published date
    # produced a list whose visible dates jumped around. The sort key must be
    # the printed value.
    from intothedarkness.notify.dates import sort_key

    early_seen_late_published = finding(discovered="2026-09-10")   # created SEEN (09-12)
    late_seen_early_published = finding(discovered="2026-08-01")
    ordered = sorted([late_seen_early_published, early_seen_late_published],
                     key=sort_key, reverse=True)
    assert [stamp_for(f).text for f in ordered] == ["2026-09-10", "2026-08-01"]


def test_an_unparseable_date_sorts_by_when_we_learned_of_it():
    # "Sep 1" has no year and gets no invented one; it takes its position from
    # discovery time rather than being forced to the top or the bottom.
    from intothedarkness.notify.dates import sort_key

    unparseable = finding(published="Sep 1")                      # created SEEN
    older = finding(discovered="2026-08-01")
    newer = finding(discovered="2026-09-13")
    ordered = sorted([older, unparseable, newer], key=sort_key, reverse=True)
    assert [stamp_for(f).label for f in ordered] == [PUBLISHED, AS_REPORTED, PUBLISHED]


def test_rfc_2822_pubdate_from_an_rss_feed_is_parsed_and_labelled_published():
    stamp = stamp_for(finding(published="Sun, 20 Sep 2026 13:21:17 +0000"))
    assert (stamp.label, stamp.text) == (PUBLISHED, "2026-09-20 13:21 UTC")
    assert stamp.when is not None
