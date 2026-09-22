"""Scraper backends. Importing this package registers the built-ins."""

from . import ctiwatch as _ctiwatch  # noqa: F401  registers the "ctiwatch" scraper
from . import darkfield as _darkfield  # noqa: F401  registers the "darkfield" scraper
from .base import REGISTRY, Scraper, available, get_scraper, register, register_function
from .dls import DlsScraper, identity_key, normalize_name
from .embedded import EmbeddedJsonScraper, extract_payloads
from .fetch import Fetcher, FetchError, Network, Response, TorNotConfigured, resolve_network
from .html import CssScraper, PageScraper
from .json_api import JsonScraper
from .rss import RssScraper

__all__ = [
    "REGISTRY",
    "CssScraper",
    "DlsScraper",
    "EmbeddedJsonScraper",
    "FetchError",
    "Fetcher",
    "JsonScraper",
    "Network",
    "PageScraper",
    "Response",
    "RssScraper",
    "Scraper",
    "TorNotConfigured",
    "available",
    "get_scraper",
    "extract_payloads",
    "identity_key",
    "normalize_name",
    "register",
    "register_function",
    "resolve_network",
]
