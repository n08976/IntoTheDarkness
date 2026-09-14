"""When a listing says it happened, and how much of that we actually know.

Two clocks run through this tool and they disagree, sometimes by months: the
date a leak site or aggregator puts on a post, and the moment we first observed
it. A report that blurs them is worse than one that omits the date, because a
reader doing incident response will act on it.

So each entry carries one of three states, and says which it is:

    published     the source gave a date we could parse without guessing
    as reported   the source gave something we will not parse -- notably
                  Everest's "Sep 1", which has no year
    first seen    the source gave nothing, so this is our own clock, labelled

The year is never inferred. "Sep 1" could be this year or five years ago, and
a report that silently picks one can misdate a breach by years.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

# Field names the sources use for their own timestamp, in order of preference.
SOURCE_TIME_FIELDS = ("discovered", "published", "date", "added")

PUBLISHED = "published"
AS_REPORTED = "as reported"
FIRST_SEEN = "first seen"

# Formats seen in the wild, all of which carry a year. Anything not listed here
# is shown verbatim rather than guessed at.
_FORMATS = (
    "%d %B %Y",      # dragonforce: "24 August 2026"
    "%d %b %Y",
    "%B %d, %Y",
    "%b %d, %Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%m/%d/%Y",
)


@dataclass(frozen=True, slots=True)
class Stamp:
    """A date for display, with its provenance and a value to sort on."""

    label: str
    text: str
    when: datetime | None

    def render(self) -> str:
        return f"{self.label} {self.text}"


def _parse(raw: str) -> datetime | None:
    """Parse only what carries an unambiguous year; otherwise give up."""
    value = raw.strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        parsed = None
    if parsed is None:
        for fmt in _FORMATS:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _format(when: datetime, had_time: bool) -> str:
    when = when.astimezone(UTC)
    return when.strftime("%Y-%m-%d %H:%M UTC") if had_time else when.strftime("%Y-%m-%d")


def stamp_for(finding) -> Stamp:  # noqa: ANN001 - Finding, avoiding a circular import
    """The best-supported date for one finding, never invented."""
    item = finding.item
    raw = ""
    if item is not None:
        for field in SOURCE_TIME_FIELDS:
            value = item.fields.get(field)
            if value:
                raw = str(value).strip()
                break

    if raw:
        when = _parse(raw)
        if when is not None:
            # A bare date has no time of day; do not imply midnight precision.
            had_time = any(sep in raw for sep in ("T", ":"))
            return Stamp(PUBLISHED, _format(when, had_time), when)
        # Parsed nothing. Show what the site said, word for word.
        return Stamp(AS_REPORTED, raw, None)

    fallback = getattr(item, "seen_at", None) or finding.created_at
    return Stamp(FIRST_SEEN, _format(fallback, had_time=True), fallback)


def sort_key(finding) -> tuple:  # noqa: ANN001 - Finding, avoiding a circular import
    """Newest first, by the date the reader will actually see.

    The list is sorted on the same value that is printed beside each entry,
    because a reader can only see the printed one: sorting on our discovery
    time while showing the site's published date produced a list whose dates
    visibly jumped around, and "mixed up" was the fair description.

    An entry whose source date could not be parsed ("Sep 1") has nothing to
    sort on, so it takes its position from when we learned of it -- roughly
    where it belongs, without inventing a date to put on it. Discovery time
    breaks ties so two posts from the same day still read newest first.
    """
    stamp = stamp_for(finding)
    primary = stamp.when or finding.created_at
    key = finding.item.key if finding.item else ""
    return (primary, finding.created_at, key)
