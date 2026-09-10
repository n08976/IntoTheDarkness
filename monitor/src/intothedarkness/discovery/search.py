"""Query onion search engines for candidate sites.

Output feeds the bookmark proposal pipeline, where a human reviews it before it
reaches a git commit. That makes precision the priority: a missed site costs
nothing, a junk proposal costs review time and can pollute a curated list.

So parsing is deliberately strict. Onions are taken only from link hrefs, never
from body text; an engine whose result container does not match yields nothing
rather than falling back to scraping every anchor on the page; and a candidate's
rank comes from how many *distinct engines* returned it, because search indexes
are spammed and a single hit is not evidence.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qs, unquote, urljoin, urlparse

from bs4 import BeautifulSoup

from ..scrapers.fetch import Fetcher, Network
from ..tor import ONION_V3_RE
from .engines import Engine, EngineHealth
from .safety import ContentFilter, default_filter

log = logging.getLogger(__name__)

ONION_IN_HREF = re.compile(r"https?://([a-z2-7]{56}\.onion)(/[^\s\"'<>]*)?", re.I)

# Titles that are navigation furniture rather than a result.
NAV_TITLES = frozenset(
    {
        "home", "search", "about", "login", "register", "next", "previous",
        "prev", "back", "contact", "help", "faq", "index", "more", "link",
        "links", "add", "submit", "advertise", "donate", "menu", "top",
    }
)
TITLE_MIN, TITLE_MAX = 4, 120


@dataclass
class Hit:
    """One result link from one engine."""

    host: str
    url: str
    title: str
    engine: str


@dataclass
class Candidate:
    """An address several engines may agree on."""

    host: str
    url: str
    titles: list[str] = field(default_factory=list)
    engines: set[str] = field(default_factory=set)

    @property
    def corroboration(self) -> int:
        return len(self.engines)

    @property
    def score(self) -> float:
        # Distinct engines dominate; distinct titles are a weak secondary signal
        # that the address is a real listing rather than a spam injection.
        return self.corroboration * 10 + min(len(set(self.titles)), 3)

    @property
    def title(self) -> str:
        if not self.titles:
            return self.host[:16]
        return min(set(self.titles), key=len)


@dataclass
class SearchReport:
    query_engines: int = 0
    responded: int = 0
    hits: int = 0
    withheld: int = 0
    candidates: list[Candidate] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)
    threshold: int = 1

    def summary(self) -> str:
        parts = [
            f"{self.responded}/{self.query_engines} engines responded",
            f"{self.hits} links",
            f"{len(self.candidates)} candidate(s)",
        ]
        if self.withheld:
            parts.append(f"{self.withheld} withheld by the content filter")
        if self.errors:
            parts.append(f"{len(self.errors)} error(s)")
        return ", ".join(parts)


def _clean_title(text: str) -> str:
    return " ".join((text or "").split())


def _plausible_title(title: str) -> bool:
    if not (TITLE_MIN <= len(title) <= TITLE_MAX):
        return False
    return title.strip().lower().strip(":»>|-") not in NAV_TITLES


def _unwrap_redirect(href: str) -> str:
    """Ahmia and friends wrap results in /redirect?redirect_url=<target>."""
    if "redirect_url=" not in href and "url=" not in href:
        return href
    query = parse_qs(urlparse(href).query)
    for param in ("redirect_url", "url", "u"):
        if param in query and query[param]:
            return unquote(query[param][0])
    return href


def parse_results(html: str, engine: Engine, base_url: str) -> list[Hit]:
    """Extract onion results from an engine's response page.

    Only ``href`` values are considered. If the engine declares a result
    container and it matches nothing, this returns nothing — a page whose layout
    we do not recognise produces no candidates rather than a page full of nav
    links.
    """
    soup = BeautifulSoup(html, "lxml")
    for junk in soup(["script", "style", "noscript"]):
        junk.decompose()

    if engine.result_selector:
        anchors = soup.select(engine.result_selector)
        if not anchors:
            log.debug("engine %s: result selector matched nothing", engine.name)
            return []
    else:
        anchors = soup.find_all("a", href=True)

    engine_host = engine.host
    hits: list[Hit] = []
    seen: set[tuple[str, str]] = set()

    for anchor in anchors:
        href = anchor.get("href") or ""
        if isinstance(href, list):
            href = href[0] if href else ""
        if not href:
            continue

        href = _unwrap_redirect(urljoin(base_url, href))
        match = ONION_IN_HREF.search(href)
        if not match:
            continue

        host = match.group(1).lower()
        if not ONION_V3_RE.match(host):
            continue
        # Self-links back into the engine are not discoveries.
        if host == engine_host or host.endswith(f".{engine_host}"):
            continue

        title = _clean_title(anchor.get_text(" ", strip=True))
        if not _plausible_title(title):
            continue

        key = (host, title.lower())
        if key in seen:
            continue
        seen.add(key)
        hits.append(Hit(host=host, url=f"http://{host}/", title=title, engine=engine.name))

    return hits


def adaptive_threshold(responded: int, requested: int | None = None) -> int:
    """How many engines must agree before a candidate is worth proposing.

    Search indexes are spammed, so a single hit is weak evidence — but demanding
    corroboration is impossible when only one engine answered. The bar rises
    with the number of engines that actually replied.
    """
    if requested is not None:
        return max(1, requested)
    return 2 if responded >= 3 else 1


def search(
    query: str,
    engines: list[Engine],
    fetcher: Fetcher,
    limit: int = 20,
    min_engines: int | None = None,
    content_filter: ContentFilter | None = None,
    force_tor: bool = True,
    known_hosts: set[str] | None = None,
    progress=None,
) -> SearchReport:
    """Query each engine in turn and collapse the results into candidates.

    Sequential on purpose: every request shares one Tor circuit, so running them
    in parallel queues them behind each other while making timeouts harder to
    attribute.
    """
    content_filter = content_filter or default_filter()
    known = known_hosts or set()
    report = SearchReport()
    hits: list[Hit] = []

    for engine in engines:
        if not engine.enabled:
            continue
        problem = engine.validate()
        if problem:
            report.errors[engine.name] = problem
            continue

        report.query_engines += 1
        url = engine.query_url(query)
        # A clearnet index is still fetched over Tor unless explicitly allowed
        # out, so a discovery run does not put the query on local DNS.
        network = Network.TOR if (force_tor or engine.is_onion) else Network.AUTO

        started = time.monotonic()
        try:
            resp = fetcher.request("GET", url, network=network)
        except Exception as exc:
            report.errors[engine.name] = str(exc)[:160]
            if progress is not None:
                progress(EngineHealth(engine.name, False, str(exc)[:80],
                                      seconds=time.monotonic() - started))
            continue

        found = parse_results(resp.text, engine, resp.url)
        report.responded += 1
        hits.extend(found)
        if progress is not None:
            progress(
                EngineHealth(
                    engine.name, True, f"{len(found)} result(s)",
                    results=len(found), seconds=time.monotonic() - started,
                )
            )

    report.hits = len(hits)

    grouped: dict[str, Candidate] = {}
    for hit in hits:
        if not content_filter.allows(hit.title, hit.url):
            report.withheld += 1
            continue
        if hit.host in known:
            continue
        candidate = grouped.get(hit.host)
        if candidate is None:
            candidate = Candidate(host=hit.host, url=hit.url)
            grouped[hit.host] = candidate
        candidate.engines.add(hit.engine)
        candidate.titles.append(hit.title)

    threshold = adaptive_threshold(report.responded, min_engines)
    report.threshold = threshold

    candidates = [c for c in grouped.values() if c.corroboration >= threshold]
    candidates.sort(key=lambda c: (-c.score, c.host))
    report.candidates = candidates[:limit]
    return report
