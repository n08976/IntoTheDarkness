"""Make addresses in a report unclickable, so a mail gateway lets it through.

Measured, not assumed: an institutional edge filter dropped every report
that carried raw .onion URLs, delivered the same report with no links at
all, and delivered it again with every URL rewritten this way. The reader
loses one-click opening and keeps everything else. Threat-intel reports
are conventionally written like this for the same reason.
"""

from __future__ import annotations

import re

_SCHEME = re.compile(r"https?://", re.I)
_TLD = re.compile(
    r"\.(onion|com|org|net|io|edu|gov|co\.uk|br|de|fr|it|es|nl|ru|ua|ch|ca|au)\b", re.I
)


def defang(text: str) -> str:
    """hxxp://example[.]com/path -- scheme and TLD broken, nothing else touched."""
    return _TLD.sub(r"[.]\1", _SCHEME.sub("hxxp://", text))
