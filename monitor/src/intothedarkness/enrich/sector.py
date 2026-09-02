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

# A usable starting vocabulary. Override wholesale via config/sectors.yaml.
DEFAULT_SECTORS: dict[str, list[str]] = {
    "healthcare": [
        "health", "hospital", "clinic", "medical", "medic", "dental", "pharma",
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

    def known(self) -> list[str]:
        return sorted(self.sectors)
