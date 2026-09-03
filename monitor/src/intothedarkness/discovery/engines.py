"""Onion search engines used for discovering candidate sites.

The catalogue lives in ``config/engines.yaml`` so it can be maintained without
touching code — these are onion addresses, and onion addresses die. Defaults
ship in-repo as a starting point, not as a promise that any of them is up.

Engine list adapted from OpenTor (github.com/vichhka-git/OpenTor, MIT).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote_plus, urlparse

import yaml

from ..tor import host_of

log = logging.getLogger(__name__)


@dataclass
class Engine:
    name: str
    url: str                      # a template containing {query}
    enabled: bool = True
    # Restrict extraction to links inside these containers. When set and the
    # selector matches nothing, the engine yields nothing rather than falling
    # back to scraping every anchor on the page.
    result_selector: str | None = None
    notes: str = ""

    @property
    def host(self) -> str:
        return host_of(self.url)

    @property
    def is_onion(self) -> bool:
        return self.host.endswith(".onion")

    def query_url(self, query: str) -> str:
        return self.url.format(query=quote_plus(query))

    def validate(self) -> str | None:
        if "{query}" not in self.url:
            return f"{self.name}: url has no {{query}} placeholder"
        if not urlparse(self.url).scheme:
            return f"{self.name}: url has no scheme"
        return None


DEFAULT_ENGINES: list[dict] = [
    {"name": "ahmia", "result_selector": "li.result a, .result a",
     "url": "http://juhanurmihxlp77nkq76byazcldy2hlmovfu2epvl5ankdibsot4csyd.onion/search/?q={query}"},
    {"name": "onionland", "result_selector": ".result-block a, .result a",
     "url": "http://3bbad7fauom4d6sgppalyqddsqbf5u5p56b5k5uk2zxsy3d6ey2jobad.onion/search?q={query}"},
    {"name": "amnesia", "result_selector": ".result a, .search-result a",
     "url": "http://amnesia7u5odx5xbwtpnqk3edybgud5bmiagu75bnqx2crntw5kry7ad.onion/search?query={query}"},
    {"name": "torland", "result_selector": ".result a",
     "url": "http://torlbmqwtudkorme6prgfpmsnile7ug2zm4u3ejpcncxuhpu4k2j4kyd.onion/index.php?a=search&q={query}"},
    {"name": "excavator", "result_selector": ".result a",
     "url": "http://2fd6cemt4gmccflhm6imvdfvli3nf7zn6rfrwpsy7uhxrgbypvwf5fad.onion/search?query={query}"},
    {"name": "onionway", "result_selector": ".result a",
     "url": "http://oniwayzz74cv2puhsgx4dpjwieww4wdphsydqvf5q7eyz4myjvyw26ad.onion/search.php?s={query}"},
    {"name": "tor66", "result_selector": None,
     "url": "http://tor66sewebgixwhcqfnp5inzp5x5uohhdy3kvtnyfxc2e5mxiuh34iid.onion/search?q={query}"},
    {"name": "oss", "result_selector": ".result a",
     "url": "http://3fzh7yuupdfyjhwt3ugzqqof6ulbcl27ecev33knxe3u7goi3vfn2qqd.onion/oss/index.php?search={query}"},
    {"name": "torgol", "result_selector": ".result a",
     "url": "http://torgolnpeouim56dykfob6jh5r2ps2j73enc42s2um4ufob3ny4fcdyd.onion/?q={query}"},
    {"name": "deepsearches", "result_selector": ".result a",
     "url": "http://searchgf7gdtauh7bhnbyed4ivxqmuoat3nm6zfrg3ymkq6mtnpye3ad.onion/search?q={query}"},
    {"name": "ddg-onion", "result_selector": None,
     "url": "https://duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion/?q={query}&ia=web"},
    # Clearnet index. Still fetched over Tor by default so a discovery run does
    # not put the query on the local network's DNS.
    {"name": "ahmia-clearnet", "result_selector": "li.result a, .result a",
     "url": "https://ahmia.fi/search/?q={query}",
     "notes": "clearnet host; routed over Tor unless you opt out"},
]


def default_engines() -> list[Engine]:
    return [Engine(**entry) for entry in DEFAULT_ENGINES]


def load_engines(path: Path | None) -> list[Engine]:
    """Read the catalogue from YAML, falling back to the built-in defaults."""
    if path is None or not Path(path).exists():
        return default_engines()

    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    entries = raw.get("engines", raw) if isinstance(raw, dict) else raw
    if not isinstance(entries, list) or not entries:
        return default_engines()

    engines: list[Engine] = []
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("name") or not entry.get("url"):
            continue
        engines.append(
            Engine(
                name=str(entry["name"]),
                url=str(entry["url"]),
                enabled=bool(entry.get("enabled", True)),
                result_selector=entry.get("result_selector") or None,
                notes=str(entry.get("notes") or ""),
            )
        )
    return engines or default_engines()


def extra_block_terms(path: Path | None) -> list[str]:
    """Optional additional content-filter terms carried in the same file."""
    if path is None or not Path(path).exists():
        return []
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        return []
    terms = raw.get("block_terms") or []
    return [str(t) for t in terms] if isinstance(terms, list) else []


@dataclass
class EngineHealth:
    engine: str
    ok: bool
    detail: str
    results: int = 0
    seconds: float = 0.0
    extra: dict = field(default_factory=dict)
