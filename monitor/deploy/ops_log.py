"""Record an operational event for the site's Diagnostics section.

    python ops_log.py <kind> "<summary>" ["<detail>"]

Replaces mailing about Tor and sweep trouble: the machine heals itself,
and the record of what it did is kept here and shown on the site.
"""

from __future__ import annotations

import sys

from intothedarkness import diag
from intothedarkness.config import get_settings


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: ops_log.py KIND SUMMARY [DETAIL]", file=sys.stderr)
        return 2
    kind, summary = sys.argv[1], sys.argv[2]
    detail = sys.argv[3] if len(sys.argv) > 3 else ""
    diag.record(get_settings().issues_file, kind, summary, detail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
