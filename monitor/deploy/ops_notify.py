"""Send a short operational notice through the Resend channel.

Run reports go out through the normal pipeline. This is for the things the
pipeline cannot tell you about, because they happen before or instead of a
run: Tor could not be rebuilt, the run itself failed, or Tor was found dead
and brought back. Without these you cannot tell a quiet week from a broken
scraper -- both look like an empty inbox.

    python ops_notify.py "subject" "body"
"""

from __future__ import annotations

import sys
from html import escape

from intothedarkness.config import get_settings
from intothedarkness.notify import Message, get_notifier


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: ops_notify.py SUBJECT [BODY]", file=sys.stderr)
        return 2

    subject, body = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
    notifier = get_notifier("resend", get_settings())
    ok, why = notifier.available()
    if not ok:
        print(f"resend unavailable: {why}", file=sys.stderr)
        return 1

    notifier.send(
        Message(
            subject=subject,
            text=body,
            html=f"<pre style='font:13px/1.5 ui-monospace,monospace'>{escape(body)}</pre>",
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
