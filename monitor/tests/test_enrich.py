"""IOC extraction and sector labelling."""

from __future__ import annotations

import pytest

from intothedarkness.enrich import SectorClassifier, extract, summarize
from intothedarkness.enrich.sector import UNKNOWN

BTC = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
BECH32 = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
ETH = "0x742d35Cc6634C0532925a3b844Bc454e4438f44e"
XMR = (
    "48jewbtxe4jU3MnzJFjTs3gVFWh2nRrbUHVQPPQGgTb7iHqDaCFTLpTLPts"
    "GKGXCHSMKmXQGGGDCJPRLRYyWMxLRHhP1Cm2"
)
ONION = "duckduckgogg42xjoc72x3sjasowoarfbgcmvfimaftt6twagswzczad.onion"


def test_extracts_each_indicator_type():
    text = f"mail ops@x.example pay {BTC} {BECH32} {ETH} {XMR} see {ONION}"
    found = extract(text)
    assert found["email"] == ["ops@x.example"]
    assert set(found["btc"]) == {BTC, BECH32}
    assert found["eth"] == [ETH.lower()]
    assert found["xmr"] == [XMR]
    assert found["onion"] == [ONION]


def test_pgp_block_is_flagged():
    assert extract("-----BEGIN PGP PUBLIC KEY BLOCK-----")["pgp"] == ["present"]


def test_emails_are_lowercased_and_deduplicated():
    assert extract("A@B.com a@b.com")["email"] == ["a@b.com"]


def test_asset_filenames_are_not_emails():
    assert "email" not in extract("logo@sprite.png icon@2x.jpg style@main.css")


def test_bitcoin_uri_scheme_is_picked_up():
    assert BTC in extract(f"bitcoin:{BTC}")["btc"]


def test_empty_text_yields_nothing():
    assert extract("") == {}
    assert summarize({}) == ""


def test_per_type_limit_is_enforced():
    text = " ".join(f"user{i}@x.example" for i in range(80))
    assert len(extract(text, limit_per_type=10)["email"]) == 10


def test_summarize_counts_by_type():
    assert summarize(extract(f"a@b.com {BTC}")) == "1 btc, 1 email"


# --------------------------------------------------------------------- sectors


@pytest.mark.parametrize(
    "name,sector",
    [
        ("St Mary Regional Hospital", "healthcare"),
        ("Northside Dental Clinic", "healthcare"),
        ("City of Springfield", "government"),
        ("Bright Futures Academy", "education"),
        ("Nordland Capital Partners", "finance"),
        ("Smith & Jones LLP", "legal"),
        ("Acme Steel Manufacturing", "manufacturing"),
        ("Blue Ridge Logistics", "transport"),
        ("Sunrise Solar Energy", "energy"),
    ],
)
def test_classifies_common_names(name, sector):
    assert SectorClassifier().classify(name) == sector


def test_unmatched_names_are_unknown_not_guessed():
    assert SectorClassifier().classify("Zzyzx Holdings") == UNKNOWN


def test_word_boundaries_prevent_substring_matches():
    # "care" must not fire on "scarecrow"; "law" must not fire on "lawn".
    classifier = SectorClassifier(sectors={"healthcare": ["care"], "legal": ["law"]})
    assert classifier.classify("Scarecrow Media") == UNKNOWN
    assert classifier.classify("Lawn Care Services") == "healthcare"


def test_longer_keywords_win_ties():
    classifier = SectorClassifier(
        sectors={"generic": ["credit"], "finance": ["credit union"]}
    )
    assert classifier.classify("Riverside Credit Union") == "finance"


def test_context_is_only_a_fallback():
    classifier = SectorClassifier()
    # Name matches nothing, context does.
    assert classifier.classify("Zzyzx Ltd", "a regional hospital group") == "healthcare"
    # Name wins when both match.
    assert classifier.classify("Zzyzx Bank", "hospital") == "finance"


def test_loads_keywords_from_yaml(tmp_path):
    path = tmp_path / "sectors.yaml"
    path.write_text("sectors:\n  defence: [defence, defense, munitions]\n")
    classifier = SectorClassifier.load(path)
    assert classifier.known() == ["defence"]
    assert classifier.classify("Northrop Munitions") == "defence"


def test_missing_or_empty_yaml_falls_back_to_builtins(tmp_path):
    assert len(SectorClassifier.load(tmp_path / "absent.yaml").known()) > 10
    empty = tmp_path / "empty.yaml"
    empty.write_text("sectors: {}\n")
    assert len(SectorClassifier.load(empty).known()) > 10
