"""Sector provenance: telling a stated fact from a guess about a name."""

from __future__ import annotations

import pytest

from intothedarkness.enrich import SectorClassifier, SectorIndex
from intothedarkness.enrich.index import index_key, usable_key
from intothedarkness.enrich.sector import (
    SOURCE_DOMAIN,
    SOURCE_NAME,
    SOURCE_NONE,
    SOURCE_PROPAGATED,
    SOURCE_TARGET,
    SOURCE_UPSTREAM,
    UNKNOWN,
    SectorResult,
)


@pytest.fixture
def classifier() -> SectorClassifier:
    return SectorClassifier()


@pytest.fixture
def index() -> SectorIndex:
    return SectorIndex(
        entries={
            "american addiction centers": "healthcare",
            "easterseals foundation": "healthcare",
            "acme steel works": "manufacturing",
        },
        domains={"stjoeshealth.org": "healthcare"},
        sectors=["healthcare"],
    )


# ------------------------------------------------------------------ precedence


def test_evidence_precedence(classifier, index):
    """Strongest evidence wins: configured > published > indexed > name > domain."""
    assert classifier.resolve("Mercy Hospital", target_sector="defence").source == SOURCE_TARGET
    assert classifier.resolve("Zzyzx", upstream="Healthcare").source == SOURCE_UPSTREAM
    assert classifier.resolve("Easterseals Foundation", index=index).source == SOURCE_PROPAGATED
    assert classifier.resolve("Mercy Hospital").source == SOURCE_NAME
    assert classifier.resolve("SJH", domain="stjoeshealth.org").source == SOURCE_DOMAIN
    assert classifier.resolve("Zzyzx Holdings").source == SOURCE_NONE


def test_a_guess_never_displaces_a_stated_fact(classifier, index):
    # The index says manufacturing; the name says nothing. Index wins.
    result = classifier.resolve("Acme Steel Works", index=index)
    assert (result.sector, result.source) == ("manufacturing", SOURCE_PROPAGATED)
    # Upstream outranks the index.
    assert classifier.resolve(
        "Acme Steel Works", upstream="Technology", index=index
    ).source == SOURCE_UPSTREAM


def test_authoritative_flags_only_stated_facts():
    assert SectorResult("healthcare", SOURCE_UPSTREAM).authoritative
    assert SectorResult("healthcare", SOURCE_PROPAGATED).authoritative
    assert SectorResult("healthcare", SOURCE_TARGET).authoritative
    assert not SectorResult("healthcare", SOURCE_NAME).authoritative
    assert not SectorResult("healthcare", SOURCE_DOMAIN).authoritative


def test_beats_orders_by_evidence_strength():
    upstream = SectorResult("healthcare", SOURCE_UPSTREAM)
    guess = SectorResult("finance", SOURCE_NAME)
    nothing = SectorResult(UNKNOWN, SOURCE_NONE)
    assert upstream.beats(guess)
    assert not guess.beats(upstream)
    assert guess.beats(nothing)
    assert upstream.beats(None)


# ---------------------------------------------------------------------- index


def test_index_lookup_by_name_and_domain(index):
    assert index.lookup("American Addiction Centers") == "healthcare"
    assert index.lookup("american addiction centers") == "healthcare"
    assert index.lookup("Unrelated Ltd") == UNKNOWN
    assert index.lookup("Anything", domain="www.stjoeshealth.org") == "healthcare"


@pytest.mark.parametrize("name", ["Summit", "ACME", "Unknown", "", "  ", "N/A"])
def test_generic_names_are_never_matched(name):
    """A wrong sector silently misroutes, and short names collide across
    dozens of unrelated companies."""
    assert not usable_key(index_key(name))


@pytest.mark.parametrize(
    "name", ["American Addiction Centers", "St. Mary's Hospital", "Café Médical"]
)
def test_distinctive_names_are_matchable(name):
    assert usable_key(index_key(name))


def test_index_keys_ignore_case_punctuation_and_accents():
    assert index_key("St. Joseph's Hôpital") == index_key("ST JOSEPHS HOPITAL")


def test_empty_index_changes_nothing(classifier):
    empty = SectorIndex()
    assert classifier.resolve("Mercy Hospital", index=empty).source == SOURCE_NAME


def test_index_round_trips_to_disk(index, settings):
    settings.ensure_dirs()
    index.save(settings)
    reloaded = SectorIndex.load(settings)
    assert reloaded.entries == index.entries
    assert reloaded.lookup("Easterseals Foundation") == "healthcare"


def test_unreadable_index_is_ignored_not_fatal(settings):
    settings.ensure_dirs()
    SectorIndex.path(settings).write_text("{not json")
    assert len(SectorIndex.load(settings)) == 0


# --------------------------------------------------------------------- domain


@pytest.mark.parametrize(
    "domain,expected",
    [
        ("stjoeshealth.org", "healthcare"),
        ("regionalmedicalcenter.com", "healthcare"),
        ("cityofboston.gov", "government"),
        ("mit.edu", "education"),
        ("paylogix.com", UNKNOWN),
        ("thecarepackage.com", UNKNOWN),   # "care" is too short to match on
        ("", UNKNOWN),
        ("notadomain", UNKNOWN),
    ],
)
def test_domain_classification(classifier, domain, expected):
    assert classifier.classify_domain(domain) == expected
