"""Make addresses in a report unclickable, so a mail gateway lets it through.

Measured, not assumed: an institutional edge filter dropped every report
that carried raw .onion URLs, delivered the same report with no links at
all, and delivered it again with every URL rewritten this way. The reader
loses one-click opening and keeps everything else. Threat-intel reports
are conventionally written like this for the same reason.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import replace

_SCHEME = re.compile(r"https?://", re.I)
_TLD = re.compile(
    r"\.(onion|com|org|net|io|edu|gov|co\.uk|br|de|fr|it|es|nl|ru|ua|ch|ca|au)\b", re.I
)


def defang(text: str) -> str:
    """hxxp://example[.]com/path -- scheme and TLD broken, nothing else touched."""
    return _TLD.sub(r"[.]\1", _SCHEME.sub("hxxp://", text))


# A .onion URL anywhere in a string -- scheme + the ".onion" of the host.
_ONION_URL = re.compile(r"https?://([a-z2-7-]+(?:\.[a-z0-9-]+)*\.onion)", re.I)


def defang_onion(text: str) -> str:
    """De-fang only hidden-service URLs, leaving clearnet links clickable.

    The public site keeps .onion links unclickable (you cannot reach them from
    a normal browser, and a live onion link on an indexable page is not wanted)
    while X, news and victim-site links stay clickable so the reader can open
    the article.
    """
    def _break(m: re.Match[str]) -> str:
        return "hxxp://" + m.group(1).replace(".onion", "[.]onion")

    return _ONION_URL.sub(_break, text)


def needs_defang(address: str, rules: Sequence[str]) -> bool:
    """Match an address against configured addresses or bare domains."""
    addr = address.strip().lower()
    for rule in rules:
        r = rule.strip().lower()
        if r == "*" or r == addr or (r and "@" not in r and addr.endswith("@" + r)):
            return True
    return False


def split_recipients(
    recipients: Sequence[str], rules: Sequence[str]
) -> tuple[list[str], list[str]]:
    """(those who get real links, those who get de-fanged ones), order kept."""
    raw = [a for a in recipients if not needs_defang(a, rules)]
    fanged = [a for a in recipients if needs_defang(a, rules)]
    return raw, fanged


def defanged(message):  # noqa: ANN001 - Message, avoiding a circular import
    """A copy of the message with every URL in text and html rewritten."""
    return replace(message, text=defang(message.text or ""), html=defang(message.html or ""))
