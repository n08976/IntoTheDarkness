"""SEC EDGAR: 8-K filings by watchlist vendors that disclose a cyber incident.

Since December 2023 a material cybersecurity incident must be disclosed on
Form 8-K under Item 1.05, and companies also describe incidents under
Item 8.01 (other events) or 7.01. Two routes cover both:

  1. For every vendor that is an SEC filer, the submissions feed lists recent
     filings with their item codes, so Item 1.05 is an exact match.
  2. EDGAR full-text search finds 8-Ks whose text mentions an incident under
     any item; hits are kept only for filers on the vendor list.

Vendors map to filers by name against the SEC's ticker list: a multi-word
name may be a prefix of the filer's, a single word must be the filer's first
word followed by nothing or a generic word ("Abbott" -> ABBOTT LABORATORIES,
not "Array" -> anything beginning with it). Most vendors are private and map
to nothing; that is expected.

EDGAR asks for a User-Agent naming a contact and at most ten requests a
second. ITD_SEC_USER_AGENT supplies the first; the scraper spaces requests
and caches the ticker list for a day and each filer's submissions for
ITD_SEC_REFRESH_MINUTES, so an hourly sweep costs nothing between refreshes.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ..models import Item, Target
from ..watchlist import Vendor, load, normalise
from .base import register_function
from .fetch import Fetcher

log = logging.getLogger(__name__)

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
FTS_URL = "https://efts.sec.gov/LATEST/search-index"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{doc}"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"

CYBER_ITEM = "1.05"
# Incident wording found by text search counts only under these items. An
# earnings release (2.02) or investor deck (7.01) mentions cybersecurity as
# a risk factor as a matter of course; measured on a real list, three of six
# hits were exactly that.
INCIDENT_ITEMS = {"1.05", "8.01"}
CYBER_QUERIES = ('"cybersecurity incident"', '"unauthorized access"', "ransomware")
# What may follow a single-word vendor in a filer's name and still be the
# same company: pure corporate form, nothing descriptive. "Abbott" is ABBOTT
# LABORATORIES; "Array" is not Array Technologies, and "Dell" is not Dell
# Technologies either -- that name belongs on the list in full.
GENERIC = {
    "laboratories", "labs", "inc", "corp", "corporation", "co", "company", "incorporated",
    "holdings", "holding", "plc", "ltd", "limited", "sa", "nv", "ag", "se",
}
SPACING = 0.15  # seconds between EDGAR requests

Fetch = Callable[[str, dict], str]


def map_vendors(vendors: list[Vendor], companies: list[dict]) -> dict[int, tuple[str, str, str]]:
    """cik -> (vendor name, filer title, ticker); one entry per filer."""
    out: dict[int, tuple[str, str, str]] = {}
    for v in vendors:
        for comp in companies:
            cik = int(comp["cik_str"])
            if cik in out:
                continue
            tt = normalise(comp["title"])
            for form in v.forms:
                if not form or not tt:
                    continue
                if tt == form or (len(form) > 1 and tt[: len(form)] == form):
                    out[cik] = (v.name, comp["title"], comp["ticker"])
                    break
                if len(form) == 1 and tt[0] == form[0] and all(t in GENERIC for t in tt[1:]):
                    out[cik] = (v.name, comp["title"], comp["ticker"])
                    break
    return out


class Edgar:
    def __init__(self, fetch: Fetch, cache_dir: Path, refresh_minutes: int, now=None):
        self.fetch, self.cache, self.refresh = fetch, cache_dir, refresh_minutes
        self.now = now or (lambda: datetime.now(UTC))
        self.cache.mkdir(parents=True, exist_ok=True)

    def _cached(self, name: str, url: str, params: dict, max_age: timedelta) -> dict:
        path = self.cache / name
        if path.exists():
            age = self.now() - datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
            if age < max_age:
                return json.loads(path.read_text(encoding="utf-8"))
        time.sleep(SPACING)
        text = self.fetch(url, params)
        data = json.loads(text)
        path.write_text(json.dumps(data), encoding="utf-8")
        return data

    def companies(self) -> list[dict]:
        return list(self._cached("tickers.json", TICKERS_URL, {}, timedelta(days=1)).values())

    def submissions(self, cik: int) -> dict:
        return self._cached(
            f"cik-{cik}.json", SUBMISSIONS_URL.format(cik=cik), {},
            timedelta(minutes=self.refresh),
        )

    def fulltext(self, query: str, start: str, end: str) -> list[dict]:
        params = {"q": query, "forms": "8-K", "dateRange": "custom", "startdt": start, "enddt": end}
        data = self._cached(
            f"fts-{abs(hash((query, start, end)))}.json", FTS_URL, params,
            timedelta(minutes=self.refresh),
        )
        hits = data.get("hits", {}).get("hits", [])
        return [h["_source"] | {"_id": h.get("_id", "")} for h in hits]


def _acc_path(accession: str) -> str:
    return accession.replace("-", "")


def collect(
    edgar: Edgar, vendors: list[Vendor], window_days: int, target_name: str
) -> list[Item]:
    filers = map_vendors(vendors, edgar.companies())
    since = (edgar.now() - timedelta(days=window_days)).date()
    items: dict[str, Item] = {}

    def add(cik: int, acc: str, filed: str, form: str, codes: str, doc: str, why: str) -> None:
        vendor, title, ticker = filers[cik]
        url = FILING_URL.format(cik=cik, acc=_acc_path(acc), doc=doc) if doc else \
            INDEX_URL.format(cik=cik, acc=_acc_path(acc))
        cyber = CYBER_ITEM in codes.split(",")
        summary = (
            f"{form} filed {filed} · items {codes or '?'} · "
            + ("Item 1.05 material cybersecurity incident" if cyber else why)
        )
        items.setdefault(acc, Item(
            key=acc, target=target_name, title=title, url=url, text=summary,
            fields={
                "watchlist": vendor, "form": form, "items": codes, "published": filed,
                "cik": str(cik), "ticker": ticker, "website": "",
                "status": "Item 1.05" if cyber else "cyber wording",
            },
        ))

    # Route 1: every mapped filer's recent 8-Ks, exact on Item 1.05.
    for cik in sorted(filers):
        try:
            recent = edgar.submissions(cik)["filings"]["recent"]
        except Exception as exc:
            log.warning("EDGAR submissions for CIK %s failed: %s", cik, exc)
            continue
        for i, form in enumerate(recent["form"]):
            if not form.startswith("8-K"):
                continue
            filed = recent["filingDate"][i]
            if datetime.fromisoformat(filed).date() < since:
                continue
            codes = recent["items"][i] or ""
            if CYBER_ITEM in codes.split(","):
                add(cik, recent["accessionNumber"][i], filed, form, codes,
                    recent["primaryDocument"][i], "")

    # Route 2: incident wording under any item, kept for mapped filers only.
    start, end = since.isoformat(), edgar.now().date().isoformat()
    for query in CYBER_QUERIES:
        try:
            hits = edgar.fulltext(query, start, end)
        except Exception as exc:
            log.warning("EDGAR full-text search %r failed: %s", query, exc)
            continue
        for h in hits:
            ciks = [int(c) for c in h.get("ciks", []) if str(c).isdigit()]
            hit_cik = next((c for c in ciks if c in filers), None)
            if hit_cik is None or not (set(h.get("items", [])) & INCIDENT_ITEMS):
                continue
            acc, _, doc = (h.get("_id") or "").partition(":")
            add(hit_cik, acc or h.get("adsh", ""), h.get("file_date", ""), h.get("form", "8-K"),
                ",".join(h.get("items", [])), doc, f"matched {query}")
    return list(items.values())


@register_function("sec")
def scrape(target: Target, fetcher: Fetcher) -> list[Item]:
    s = fetcher.settings
    if not s.sec_user_agent:
        raise RuntimeError(
            "ITD_SEC_USER_AGENT is not set; EDGAR requires a User-Agent naming a contact"
        )
    vendors = load(s.watchlist_file)
    if not vendors:
        return []

    def fetch(url: str, params: dict) -> str:
        resp = fetcher.request(
            "GET", url, {"User-Agent": s.sec_user_agent, "Accept": "application/json"},
            params or None, None, network="direct",
        )
        return resp.text

    edgar = Edgar(fetch, s.data_dir / "sec", s.sec_refresh_minutes)
    return collect(edgar, vendors, s.sec_window_days, target.name)
