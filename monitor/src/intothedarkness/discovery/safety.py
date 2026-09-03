"""Content filtering for discovery results.

Searching dark web indexes surfaces material that has no intelligence value and
that nobody should be shown unprompted. Results matching this filter are
withheld, and the *count* is always reported — so there is no silent blind spot,
but there is also no switch that prints the withheld titles back out.

That layering is deliberate. A transport-layer filter that drops silently
destroys the ability to say what you did and did not see; a filter with a
"show me anyway" flag defeats its own purpose. Reporting the count is the
honest middle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Unambiguous indicators of material that is illegal to access and worthless as
# intelligence. Kept deliberately narrow: broad filtering on a CTI tool creates
# blind spots. Extend via `config/engines.yaml` if a directory in your scope
# uses different shorthand.
_BLOCK_TERMS = frozenset(
    {
        "csam", "cp video", "child porn", "childporn", "pedo", "paedo",
        "preteen", "lolita city", "jailbait", "hurtcore", "snuff",
        "rape video", "torture video", "no limits fun",
    }
)

_BLOCK_RE = re.compile(
    "|".join(re.escape(term) for term in sorted(_BLOCK_TERMS)), re.I
)


@dataclass(frozen=True)
class Verdict:
    blocked: bool
    reason: str = ""


class ContentFilter:
    """Decides whether a discovery result should be withheld."""

    def __init__(self, extra_terms: list[str] | None = None) -> None:
        terms = set(_BLOCK_TERMS)
        terms.update(t.strip().lower() for t in (extra_terms or []) if t.strip())
        self._pattern = re.compile(
            "|".join(re.escape(term) for term in sorted(terms)), re.I
        )

    def check(self, *parts: str) -> Verdict:
        haystack = " ".join(p for p in parts if p)
        match = self._pattern.search(haystack)
        if match is None:
            return Verdict(False)
        # The matched term is named, never the surrounding content.
        return Verdict(True, f"matched content filter ({match.group(0).lower()})")

    def allows(self, *parts: str) -> bool:
        return not self.check(*parts).blocked


def default_filter() -> ContentFilter:
    return ContentFilter()
