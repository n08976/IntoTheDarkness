"""Indicator extraction from scraped text.

Deliberately conservative: a false positive here becomes an alert someone has
to triage, so patterns are anchored and obviously-bogus matches are dropped.
"""

from __future__ import annotations

import re
from collections import defaultdict

# Emails. Not RFC-complete on purpose; the long tail is mostly false positives.
EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,24}\b")

# Bitcoin: legacy/P2SH base58 plus bech32. Base58 excludes 0, O, I and l.
BTC_LEGACY_RE = re.compile(r"\b[13][a-km-zA-HJ-NP-Z1-9]{25,34}\b")
BTC_BECH32_RE = re.compile(r"\bbc1[023456789acdefghjklmnpqrstuvwxyz]{11,71}\b")
ETH_RE = re.compile(r"\b0x[a-fA-F0-9]{40}\b")
# Monero: 95 characters starting with 4 or 8, or a 106-character integrated address.
XMR_RE = re.compile(r"\b[48][0-9AB][1-9A-HJ-NP-Za-km-z]{93}(?:[1-9A-HJ-NP-Za-km-z]{11})?\b")

ONION_RE = re.compile(r"\b[a-z2-7]{56}\.onion\b", re.I)
PGP_RE = re.compile(
    r"-----BEGIN PGP (?:PUBLIC KEY BLOCK|PRIVATE KEY BLOCK|MESSAGE|SIGNATURE)-----"
)
BITCOIN_URI_RE = re.compile(r"\bbitcoin:([a-zA-Z0-9]{25,90})\b")

# Extensions that would otherwise look like an email domain (foo@bar.png).
_IMAGE_TLDS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "css", "js"}

IOC_TYPES = ("email", "btc", "eth", "xmr", "onion", "pgp")


def _clean_emails(text: str) -> list[str]:
    out = []
    for match in EMAIL_RE.findall(text):
        tld = match.rsplit(".", 1)[-1].lower()
        if tld in _IMAGE_TLDS:
            continue
        out.append(match.lower())
    return out


def extract(text: str, limit_per_type: int = 50) -> dict[str, list[str]]:
    """Pull indicators out of a blob of text, deduplicated and order-stable."""
    if not text:
        return {}

    found: dict[str, list[str]] = defaultdict(list)
    seen: dict[str, set[str]] = defaultdict(set)

    def add(kind: str, value: str) -> None:
        if value in seen[kind] or len(found[kind]) >= limit_per_type:
            return
        seen[kind].add(value)
        found[kind].append(value)

    for value in _clean_emails(text):
        add("email", value)
    for value in BTC_LEGACY_RE.findall(text):
        add("btc", value)
    for value in BTC_BECH32_RE.findall(text):
        add("btc", value.lower())
    for value in BITCOIN_URI_RE.findall(text):
        add("btc", value)
    for value in ETH_RE.findall(text):
        add("eth", value.lower())
    for value in XMR_RE.findall(text):
        add("xmr", value)
    for value in ONION_RE.findall(text):
        add("onion", value.lower())
    if PGP_RE.search(text):
        add("pgp", "present")

    return dict(found)


def summarize(iocs: dict[str, list[str]]) -> str:
    """A one-line count, e.g. ``2 email, 1 btc``."""
    if not iocs:
        return ""
    return ", ".join(f"{len(v)} {k}" for k, v in sorted(iocs.items()) if v)
