"""Leak-site scraper: extract victim organisation names from a listing page.

Identity is the *normalised company name*, not the URL. Leak sites rotate onion
addresses and reshuffle paths constantly, so keying on a link would report the
same victim as new every time a mirror changed. Keying on the name means a
victim is reported once, wherever it later appears.
"""

from __future__ import annotations

import re
import unicodedata

from ..enrich import ioc
from ..models import Item, Selectors, Target, stable_hash
from .base import Scraper, register
from .html import _link_of, _pick, _soup, _text_of

# Boilerplate that clings to a victim name in listing markup.
_NOISE = re.compile(
    r"\b("
    r"read\s*more|view\s*(more|details)|download(\s*(now|all|data))?|"
    r"published|disclosed|leaked|full\s*data|preview|visit\s*site|"
    r"click\s*here|show\s*more|details|expired|timer|deadline"
    r")\b",
    re.I,
)
_URL_IN_NAME = re.compile(r"https?://\S+|\b[a-z0-9-]+\.(?:com|net|org|co\.uk|de|fr|io)\b", re.I)
_DATE = re.compile(r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b")
_SIZE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:[KMGT]B|bytes)\b", re.I)
_PERCENT = re.compile(r"\b\d{1,3}\s*%")
_BRACKETS = re.compile(r"[\[\](){}<>]")
_WS = re.compile(r"\s+")

# Suffixes stripped only for the identity key, never from the displayed name,
# so "Acme Ltd" and "Acme Limited" are recognised as one victim.
_LEGAL_SUFFIX = re.compile(
    r"\b("
    r"inc|inc\.|incorporated|llc|l\.l\.c\.|ltd|ltd\.|limited|llp|plc|corp|corp\.|"
    r"corporation|co|co\.|company|gmbh|ag|kg|bv|nv|sa|sas|srl|spa|pty|pte|"
    r"oy|ab|as|aps|holding|holdings|group|international"
    r")\b\.?",
    re.I,
)


# A listing sometimes gives the victim's domain instead of its name.
_BARE_DOMAIN = re.compile(
    r"^\s*(?:https?://)?(?:www\.)?([a-z0-9][a-z0-9-]{1,62})\.[a-z.]{2,12}\s*$", re.I
)
# What is left when URL-stripping eats the whole name.
_STUB = frozenset({"www", "http", "https", "com", "net", "org", "inc", "ltd"})


def normalize_name(raw: str) -> str:
    """Clean a victim name for display: strip site furniture, keep the name.

    An entry whose "name" is just a domain becomes the domain's own label —
    `www.rubbermill.com` reads as `Rubbermill`, not as the stub `www` that
    URL-stripping would otherwise leave behind.
    """
    text = unicodedata.normalize("NFKC", raw or "")

    domain = _BARE_DOMAIN.match(text)
    if domain:
        return domain.group(1).replace("-", " ").title()

    text = _URL_IN_NAME.sub(" ", text)
    text = _NOISE.sub(" ", text)
    text = _DATE.sub(" ", text)
    text = _SIZE.sub(" ", text)
    text = _PERCENT.sub(" ", text)
    text = _BRACKETS.sub(" ", text)
    text = text.replace("|", " ").replace("•", " ")
    text = _WS.sub(" ", text).strip(" -–—:,.\t\n")
    # Stripping a URL can consume the whole name; that is not a victim.
    if text.lower() in _STUB:
        return ""
    return text


def identity_key(name: str) -> str:
    """A stable key for a victim, tolerant of casing and legal-suffix drift."""
    text = unicodedata.normalize("NFKD", name.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _LEGAL_SUFFIX.sub(" ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return _WS.sub(" ", text).strip()


@register
class DlsScraper(Scraper):
    """Victim listings on a leak site.

    ``selectors.item`` picks each victim entry; ``title`` is the organisation
    name. ``text``, ``link`` and ``attrs`` are optional context carried along
    for the report. Set ``sector`` on the target to label every victim, or let
    the classifier infer one per name.
    """

    name = "dls"

    def scrape(self, target: Target) -> list[Item]:
        resp = self.fetcher.request(
            target.method,
            target.url,
            target.headers,
            target.params,
            target.body,
            network=target.network,
        )
        soup = _soup(resp.text)
        sel: Selectors = target.selectors

        if not sel.item:
            raise ValueError(
                f"target {target.name!r} uses the dls scraper but sets no "
                "selectors.item; it must point at each victim entry"
            )

        classifier = self.classifier
        items: list[Item] = []
        seen: set[str] = set()

        for node in soup.select(sel.item):
            raw_name = _text_of(_pick(node, sel.title)) if sel.title else _text_of(node)
            company = normalize_name(raw_name)
            if not company:
                continue

            key_source = identity_key(company)
            if not key_source or key_source in seen:
                continue  # the same victim listed twice on one page
            seen.add(key_source)

            context = _text_of(_pick(node, sel.text)) if sel.text else _text_of(node)
            link = _link_of(node, sel.link, resp.url) if sel.link else ""

            fields: dict[str, object] = {"company": company}
            for field_name, spec in sel.attrs.items():
                selector, _, attr = spec.partition("@")
                sub = node.select_one(selector) if selector else node
                if sub is None:
                    continue
                if attr:
                    value = sub.get(attr)
                    fields[field_name] = (
                        " ".join(value) if isinstance(value, list) else (value or "")
                    )
                else:
                    fields[field_name] = _text_of(sub)

            if target.sector:
                fields["sector"] = target.sector
            elif classifier is not None:
                fields["sector"] = classifier.classify(company, context)  # context off by default

            indicators = ioc.extract(context)
            if indicators:
                fields["iocs"] = indicators

            items.append(
                Item(
                    key=stable_hash("dls", key_source),
                    target=target.name,
                    title=company,
                    url=link,
                    text=context,
                    fields=fields,
                )
            )

        return self.filter(target, items)
