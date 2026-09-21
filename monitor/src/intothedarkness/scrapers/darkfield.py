"""Darkfield (darkfield.orizon.one): a ransomware-disclosure tracker.

Its API needs a login, but /feed.xml is public and carries the 60 most
recent disclosures. Each entry packs the facts into prose --

    title        "Hudson MD Group, LLC — claimed by metaencryptor"
    description  "Healthcare · US · data_published"

-- so this unpacks them: the victim becomes the item title (what the
classifier and the cross-source dedupe both key on), and operator, sector,
country and status become fields. The sector is Darkfield's own label, which
the pipeline treats as an upstream fact rather than a guess from the name.
"""

from __future__ import annotations

import re

from ..models import Item, Target
from .base import register_function
from .fetch import Fetcher
from .rss import RssScraper

_CLAIMED = re.compile(r"^(?P<victim>.+?)\s+[—–-]+\s+claimed by\s+(?P<group>.+?)\s*$", re.I)
_COUNTRY = re.compile(r"^[A-Z]{2}$")


def _unpack(item: Item) -> Item:
    victim, group = item.title, ""
    m = _CLAIMED.match(item.title)
    if m:
        victim, group = m["victim"].strip(), m["group"].strip()

    sector = country = status = ""
    parts = [p.strip() for p in item.text.split("·") if p.strip()]
    if parts:
        sector = parts[0]
        for part in parts[1:]:
            if _COUNTRY.match(part):
                country = part
            else:
                status = part

    fields = dict(item.fields)
    for name, value in (
        ("group", group),
        ("sector", sector),
        ("country", country),
        ("status", status),
    ):
        if value:
            fields[name] = value

    return Item(
        key=item.key,
        target=item.target,
        title=victim,
        url=item.url,
        text=item.text,
        fields=fields,
        seen_at=item.seen_at,
    )


@register_function("darkfield")
def scrape(target: Target, fetcher: Fetcher) -> list[Item]:
    return [_unpack(item) for item in RssScraper(fetcher).scrape(target)]
