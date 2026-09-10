"""Scraper backends. Importing this package registers the built-ins."""

from .base import REGISTRY, Scraper, available, get_scraper, register, register_function
from .dls import DlsScraper, identity_key, normalize_name
from .embedded import EmbeddedJsonScraper, extract_payloads
from .fetch import Fetcher, FetchError, Network, Response, TorNotConfigured, resolve_network
from .html import CssScraper, PageScraper
from .json_api import JsonScraper

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
