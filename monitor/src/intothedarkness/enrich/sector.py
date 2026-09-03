"""Industry-sector labelling for victim names.

Keyword matching, not magic. It exists so you can ask "show me only healthcare"
without hand-reading every name. Rules live in ``config/sectors.yaml`` so the
vocabulary can grow without code changes, and anything unmatched is reported as
``unknown`` rather than guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

UNKNOWN = "unknown"

# Where a sector label came from. Routing on a label is only safe if you can
# tell an authoritative statement from a guess about a company's name.
SOURCE_TARGET = "target"      # stated in the target config
SOURCE_UPSTREAM = "upstream"  # the source published its own industry label
SOURCE_PROPAGATED = "propagated"  # an authoritative label for the same victim elsewhere
SOURCE_NAME = "name"          # keyword match on the organisation name
SOURCE_DOMAIN = "domain"      # keyword match on the victim's domain
SOURCE_NONE = "none"

# Ordered weakest to strongest; a weaker source never overwrites a stronger one.
SOURCE_RANK = {
    SOURCE_NONE: 0,
    SOURCE_DOMAIN: 1,
    SOURCE_NAME: 2,
    SOURCE_PROPAGATED: 3,
    SOURCE_UPSTREAM: 4,
    SOURCE_TARGET: 5,
}

# Sources trustworthy enough to route on without a human reading the name first.
AUTHORITATIVE = frozenset({SOURCE_TARGET, SOURCE_UPSTREAM, SOURCE_PROPAGATED})

# A usable starting vocabulary. Override wholesale via config/sectors.yaml.
DEFAULT_SECTORS: dict[str, list[str]] = {
    "healthcare": [
        "health", "hospital", "clinic", "medical", "medic", "dental", "dentist", "pharma",
        "care center", "care centre", "surgery", "orthope", "pediatric",
        "paediatric", "diagnostic", "radiolog", "oncolog", "nursing", "hospice",
        "eye care", "optical", "optometr", "bioresearch", "biotech",
    ],
    "education": [
        "school", "university", "college", "academy", "institute", "campus",
        "education", "district", "isd", "polytechnic", "kindergarten",
    ],
    "government": [
        "city of", "county", "municipal", "ministry", "department of", "gov",
        "council", "state of", "agency", "bureau", "police", "court",
    ],
    "finance": [
        "bank", "credit union", "fcu", "capital", "financial", "finance", "insur",
        "invest", "asset", "wealth", "mortgage", "lending", "accounting",
        "tax", "audit",
    ],
    "legal": ["law", "legal", "attorney", "solicitor", "advocat", "counsel", "llp"],
    "manufacturing": [
        "manufactur", "industri", "factory", "steel", "plastic", "machin",
        "fabricat", "foundry", "assembly", "components", "engineering",
    ],
    "technology": [
        "software", "technolog", "systems", "digital", "cyber", "data",
        "cloud", "hosting", "telecom", "networks", "semiconductor", "it services",
    ],
    "energy": [
        "energy", "oil", "gas", "petrol", "solar", "electric", "power",
        "utility", "utilities", "pipeline", "nuclear", "mining",
    ],
    "transport": [
        "logistic", "transport", "shipping", "freight", "airline", "airport",
        "railway", "trucking", "courier", "maritime", "port of",
    ],
    "retail": [
        "retail", "store", "shop", "market", "supermarket", "grocery",
        "fashion", "apparel", "commerce", "brands",
    ],
    "construction": [
        "construct", "building", "contractor", "roofing", "plumbing",
        "electrical services", "concrete", "architect", "civil",
    ],
    "hospitality": [
        "hotel", "resort", "restaurant", "catering", "casino", "travel",
        "tourism", "hospitality", "leisure",
    ],
    "nonprofit": ["foundation", "charity", "nonprofit", "non-profit", "ngo", "trust", "society"],
    "agriculture": ["farm", "agri", "food", "dairy", "produce", "livestock", "fisher"],
    "media": ["media", "broadcast", "publish", "news", "press", "studio", "entertainment"],
}


# Aggregators label industry themselves. Their label is better evidence than
# our keyword guess, so it is mapped onto our vocabulary rather than discarded
# or re-derived. Unmapped labels fall through to `unknown` rather than being
# invented as new sectors, which would fragment filtering.
SECTOR_ALIASES: dict[str, str] = {
    "financial services": "finance", "finance": "finance", "banking": "finance",
    "insurance": "finance", "accounting": "finance",
    "healthcare": "healthcare", "health care": "healthcare",
    "medical": "healthcare", "pharmaceuticals": "healthcare",
    "manufacturing": "manufacturing", "industrial": "manufacturing",
    "technology": "technology", "it services": "technology",
    "telecommunications": "technology", "software": "technology",
    "education": "education", "transportation": "transport",
    "logistics": "transport", "shipping": "transport",
    "retail & e-commerce": "retail", "retail": "retail", "e-commerce": "retail",
    "agriculture and food production": "agriculture", "agriculture": "agriculture",
    "food production": "agriculture",
    "energy & utilities": "energy", "energy": "energy", "utilities": "energy",
    "government & defense": "government", "government": "government",
    "defense": "government", "public sector": "government",
    "hospitality": "hospitality", "travel": "hospitality",
    "construction": "construction", "real estate": "construction",
    "legal": "legal", "law": "legal",
    "media": "media", "entertainment": "media",
    "non-profit": "nonprofit", "nonprofit": "nonprofit", "ngo": "nonprofit",
    # Explicit non-answers from upstream.
    "not found": UNKNOWN, "other": UNKNOWN, "unknown": UNKNOWN, "n/a": UNKNOWN,
}


def normalize_sector(label: str | None) -> str | None:
    """Map an externally supplied industry label onto our vocabulary.

    Returns ``None`` when the label means nothing to us, so the caller can fall
    back to classifying the name rather than inventing a one-off sector.
    """
    if not label:
        return None
    key = " ".join(str(label).strip().lower().split())
    if not key:
        return None
    if key in SECTOR_ALIASES:
        mapped = SECTOR_ALIASES[key]
        return None if mapped == UNKNOWN else mapped
    if key in DEFAULT_SECTORS:
        return key
    return None


@dataclass(frozen=True)
class SectorResult:
    """A sector label together with where it came from."""

    sector: str
    source: str = SOURCE_NONE

    @property
    def known(self) -> bool:
        return self.sector != UNKNOWN

    @property
    def authoritative(self) -> bool:
        """Whether this is a stated fact rather than a guess about a name."""
        return self.source in AUTHORITATIVE

    def beats(self, other: SectorResult | None) -> bool:
        if other is None or not other.known:
            return True
        if not self.known:
            return False
        return SOURCE_RANK[self.source] > SOURCE_RANK[other.source]


@dataclass
class SectorClassifier:
    """Longest-keyword-wins matching over a name and optional context text."""

    sectors: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SECTORS))
    _compiled: list[tuple[str, str, re.Pattern[str]]] = field(default_factory=list, init=False)

    def __post_init__(self) -> None:
        self._compile()

    def _compile(self) -> None:
        entries: list[tuple[str, str, re.Pattern[str]]] = []
        for sector, keywords in self.sectors.items():
            for keyword in keywords:
                keyword = keyword.strip().lower()
                if not keyword:
                    continue
                # Word-boundary anchored so "care" does not match "scarecrow",
                # but multi-word keywords ("city of") still work.
                pattern = re.compile(rf"(?<![a-z0-9]){re.escape(keyword)}", re.I)
                entries.append((sector, keyword, pattern))
        # Longer keywords are more specific, so let them win ties.
        entries.sort(key=lambda e: len(e[1]), reverse=True)
        self._compiled = entries

    @classmethod
    def load(cls, path: Path | None) -> SectorClassifier:
        """Read sector keywords from YAML, falling back to the built-ins."""
        if path is None or not Path(path).exists():
            return cls()
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        data = raw.get("sectors", raw) if isinstance(raw, dict) else {}
        if not isinstance(data, dict) or not data:
            return cls()
        sectors = {
            str(name): [str(k) for k in keywords]
            for name, keywords in data.items()
            if isinstance(keywords, list)
        }
        return cls(sectors=sectors or dict(DEFAULT_SECTORS))

    def classify(self, name: str, context: str = "", use_context: bool = False) -> str:
        """Label a company from its name.

        ``context`` is consulted only when ``use_context`` is set, and it is off
        by default because on a leak site the surrounding text describes *the
        stolen data*, not the victim's industry. Measured against a live listing:
        a construction firm whose description ended "...as well as financial
        documents" was labelled `finance`, and a law firm was labelled `finance`
        because family-law work mentions "asset division".

        A wrong label is worse than none — sector filtering routes alerts, so a
        mislabelled victim goes to the wrong place silently. `unknown` is the
        honest answer when the name carries no signal.
        """
        for sector, _keyword, pattern in self._compiled:
            if pattern.search(name):
                return sector
        if context and use_context:
            for sector, _keyword, pattern in self._compiled:
                if pattern.search(context):
                    return sector
        return UNKNOWN

    def classify_domain(self, domain: str) -> str:
        """Classify from a victim's own domain.

        Weaker than the organisation name but often complementary —
        `stjoeshealth.org` says what `SJH Inc` does not. The TLD alone carries
        signal only for `.gov` and `.edu`; everything else is read as tokens.
        """
        if not domain:
            return UNKNOWN
        host = domain.strip().lower().split("/")[0]
        host = host.removeprefix("www.")
        if not host or "." not in host:
            return UNKNOWN

        labels = host.split(".")
        tld = labels[-1]
        if tld == "gov" or host.endswith(".gov.uk") or ".gov." in host:
            return "government"
        if tld == "edu" or host.endswith(".ac.uk") or ".edu." in host:
            return "education"

        # Domain labels run words together ("stjoeshealth"), so the word-boundary
        # anchoring used for names cannot fire. Match as a substring instead, but
        # only for keywords long enough that an accidental hit is unlikely — the
        # domain is the weakest signal and must not manufacture false positives.
        stem = "".join(labels[:-1]).replace("-", "").replace(".", "")
        for sector, keyword, _pattern in self._compiled:
            token = keyword.replace(" ", "")
            if len(token) >= 5 and token in stem:
                return sector
        return UNKNOWN

    def resolve(
        self,
        name: str,
        upstream: str | None = None,
        domain: str = "",
        target_sector: str | None = None,
        context: str = "",
        use_context: bool = False,
        index=None,
    ) -> SectorResult:
        """Best available label, with its provenance.

        Precedence is by strength of evidence: an explicitly configured sector,
        then a label the source itself published, then the organisation name,
        then its domain. A guess never displaces a stated fact.
        """
        if target_sector:
            return SectorResult(target_sector, SOURCE_TARGET)

        mapped = normalize_sector(upstream)
        if mapped:
            return SectorResult(mapped, SOURCE_UPSTREAM)

        # A source that classifies this same victim elsewhere is a stated fact
        # about it, not a guess about its name.
        if index is not None:
            indexed = index.lookup(name, domain)
            if indexed and indexed != UNKNOWN:
                return SectorResult(indexed, SOURCE_PROPAGATED)

        by_name = self.classify(name, context, use_context=use_context)
        if by_name != UNKNOWN:
            return SectorResult(by_name, SOURCE_NAME)

        by_domain = self.classify_domain(domain)
        if by_domain != UNKNOWN:
            return SectorResult(by_domain, SOURCE_DOMAIN)

        return SectorResult(UNKNOWN, SOURCE_NONE)

    def known(self) -> list[str]:
        return sorted(self.sectors)
