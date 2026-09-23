from intothedarkness.models import Finding, FindingKind, Item
from intothedarkness.watchlist import Vendor, find_matches, normalise, parse


def vendor(name: str) -> Vendor:
    return parse(name)[0]


def test_normalise_drops_case_punctuation_and_corporate_suffixes():
    assert normalise("R1 RCM, Inc.") == ("r1", "rcm")
    assert normalise("Thermo Fisher Scientific Inc.") == ("thermo", "fisher", "scientific")
    assert normalise("Smith & Nephew") == ("smith", "and", "nephew")
    assert normalise("LivaNova USA, Inc.") == ("livanova",)


def test_a_vendor_matches_a_victim_that_starts_with_its_name():
    assert vendor("Fresenius Medical Care").matches("Fresenius Medical Care US")
    assert vendor("R1 RCM").matches("R1 RCM, Inc. (US)")
    assert not vendor("Fresenius Medical Care").matches("Care Fresenius")


def test_single_word_names_must_match_the_whole_victim_name():
    # Measured: "Olympus" as a prefix fired on "Olympus Financial"; "GE" or
    # "MRI" as prefixes would fire on half a leak site.
    assert vendor("GE").matches("GE")
    assert vendor("GE").matches("GE Inc")                    # suffix stripped, still whole
    assert not vendor("GE").matches("GE Aerospace")
    assert not vendor("MRI").matches("MRI Software LLC")
    assert not vendor("Olympus").matches("Olympus Financial")
    assert vendor("Konica Minolta").matches("Konica Minolta Bulgaria")   # two words: prefix ok


def test_parentheticals_become_aliases_and_duplicates_collapse():
    text = "Oracle (formerly Cerner)\nAbbott\nAbbott\n\"Connectwise\t\"\n# comment\n\nAbbott "
    vendors = parse(text)
    names = [v.name for v in vendors]
    assert names == ["Oracle", "Abbott", "Connectwise"]
    oracle = vendors[0]
    assert oracle.matches("Cerner Corporation") and oracle.matches("Oracle")
    # One word, so whole-name only: "Oracle Health" needs its own line.
    assert not oracle.matches("Oracle Health")


def test_find_matches_reports_the_vendor_per_finding():
    vendors = parse("Fresenius Medical Care\nAbbott")

    def f(t):
        return Finding(kind=FindingKind.NEW, target="t", item=Item(key=t, target="t", title=t))

    hits = find_matches(vendors, [f("Textile City"), f("Fresenius Medical Care US"), f("Abbott")])
    assert {i: v.name for i, v in hits.items()} == {1: "Fresenius Medical Care", 2: "Abbott"}
