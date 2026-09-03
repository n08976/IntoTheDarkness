"""Scraper contract and registry.

A scraper turns one :class:`Target` into a list of :class:`Item`. Register a new
one with the ``@register`` decorator and it becomes usable as ``scraper: <name>``
in the target YAML.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence

from ..enrich import SectorClassifier
from ..models import Item, Target
from .fetch import Fetcher


class Scraper(ABC):
    name: str = "base"

    def __init__(self, fetcher: Fetcher) -> None:
        self.fetcher = fetcher
        # Set by the pipeline. Scrapers that label industry sectors use it;
        # the rest ignore it.
        self.classifier: SectorClassifier | None = None

    @abstractmethod
    def scrape(self, target: Target) -> list[Item]:
        """Fetch the target and return the items found, newest-first if ordered."""

    def filter(self, target: Target, items: Sequence[Item]) -> list[Item]:
        """Apply the target's include/exclude regexes to extracted items."""
        out = list(items)
        if target.include:
            pattern = re.compile(target.include, re.I)
            out = [i for i in out if pattern.search(_haystack(i))]
        if target.exclude:
            pattern = re.compile(target.exclude, re.I)
            out = [i for i in out if not pattern.search(_haystack(i))]
        return out


def _haystack(item: Item) -> str:
    return "\n".join([item.title, item.url, item.text, str(item.fields)])


REGISTRY: dict[str, type[Scraper]] = {}


def register(cls: type[Scraper]) -> type[Scraper]:
    REGISTRY[cls.name] = cls
    return cls


def get_scraper(name: str, fetcher: Fetcher) -> Scraper:
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown scraper {name!r}; registered: {known}") from None
    return cls(fetcher)


def available() -> list[str]:
    return sorted(REGISTRY)


CustomScrape = Callable[[Target, Fetcher], list[Item]]


def register_function(name: str) -> Callable[[CustomScrape], CustomScrape]:
    """Register a plain function as a scraper, for one-off site-specific logic."""

    def decorator(fn: CustomScrape) -> CustomScrape:
        cls = type(
            f"{name.title().replace('_', '')}Scraper",
            (Scraper,),
            {
                "name": name,
                "scrape": lambda self, target, _fn=fn: self.filter(
                    target, _fn(target, self.fetcher)
                ),
            },
        )
        register(cls)
        return fn

    return decorator
