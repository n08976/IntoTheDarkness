"""Leak-site victim extraction and identity."""

from __future__ import annotations

import pytest
from test_scrapers import FakeFetcher

from intothedarkness.enrich import SectorClassifier
from intothedarkness.models import Selectors, Target
from intothedarkness.scrapers import get_scraper
from intothedarkness.scrapers.dls import identity_key, normalize_name

LISTING = """
<html><body>
  <div class="victim-card">
    <h3>ACME Steel Ltd [READ MORE]</h3>
    <p class="description">Manufacturing. 12.5 GB. Contact ops@acme-leak.example</p>
    <a href="/victim/acme">details</a>
  </div>
  <div class="victim-card">
    <h3>St Mary Regional Hospital | 2024-03-01 | Download Now</h3>
    <p class="description">Patient records, 80%</p>
  </div>
  <div class="victim-card">
    <h3>  </h3><p class="description">empty name, should be skipped</p>
  </div>
  <div class="victim-card">
    <h3>ACME STEEL LIMITED</h3><p class="description">duplicate of the first</p>
  </div>
</body></html>
"""


def dls_target(**kw) -> Target:
    base = dict(
        name="leaks",
        url="http://example.onion/",
        scraper="dls",
        selectors=Selectors(item=".victim-card", title="h3", text=".description"),
    )
    return Target(**{**base, **kw})


def scrape(html=LISTING, target=None, classifier=None):
    scraper = get_scraper("dls", FakeFetcher(html, url="http://example.onion/"))
    scraper.classifier = classifier or SectorClassifier()
    return scraper.scrape(target or dls_target())


# ------------------------------------------------------------------ normalising


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("ACME Steel Ltd [READ MORE]", "ACME Steel Ltd"),
        ("St Mary Hospital | 12.5 GB | 2024-03-01", "St Mary Hospital"),
        ("  Globex Corporation — Download Now  ", "Globex Corporation"),
        ("Foo Inc (published 01/02/2024)", "Foo Inc"),
        ("Bar Ltd 45%", "Bar Ltd"),
        ("Visit site: Baz Group", "Baz Group"),
    ],
)
def test_normalize_strips_site_furniture(raw, expected):
    assert normalize_name(raw) == expected


@pytest.mark.parametrize(
    "a,b",
    [
        ("Acme Steel Ltd", "ACME STEEL LIMITED"),
        ("Globex Corporation", "Globex Corp."),
        ("Foo Holdings GmbH", "Foo"),
        ("Café Group", "Cafe"),
    ],
)
def test_identity_ignores_case_accents_and_legal_suffixes(a, b):
    assert identity_key(a) == identity_key(b)


def test_identity_still_separates_genuinely_different_names():
    assert identity_key("Acme Steel") != identity_key("Acme Health")


# -------------------------------------------------------------------- scraping


def test_extracts_victims_and_skips_blanks_and_duplicates():
    items = scrape()
    assert [i.title for i in items] == ["ACME Steel Ltd", "St Mary Regional Hospital"]


def test_key_is_the_company_not_the_url():
    """A leak site that moves to a new onion must not re-report every victim."""
    first = scrape()
    moved = LISTING.replace('href="/victim/acme"', 'href="http://other.onion/v/1"')
    second = scrape(moved)
    assert [i.key for i in first] == [i.key for i in second]


def test_sector_is_inferred_per_victim():
    items = scrape()
    assert items[0].sector == "manufacturing"
    assert items[1].sector == "healthcare"


def test_target_sector_overrides_the_classifier():
    items = scrape(target=dls_target(sector="defence"))
    assert {i.sector for i in items} == {"defence"}


def test_indicators_are_extracted_from_context():
    items = scrape()
    assert items[0].fields["iocs"]["email"] == ["ops@acme-leak.example"]
    assert "iocs" not in items[1].fields


def test_include_filter_narrows_victims():
    items = scrape(target=dls_target(include="hospital"))
    assert [i.title for i in items] == ["St Mary Regional Hospital"]


def test_missing_item_selector_is_a_clear_error():
    target = dls_target(selectors=Selectors(title="h3"))
    with pytest.raises(ValueError, match="selectors.item"):
        scrape(target=target)


def test_company_is_carried_in_fields():
    assert scrape()[0].fields["company"] == "ACME Steel Ltd"


# ---------------------------------------------------- regressions from live data


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("www.rubbermill.com", "Rubbermill"),
        ("rubbermill.com", "Rubbermill"),
        ("http://www.acme-steel.co.uk", "Acme Steel"),
    ],
)
def test_bare_domain_names_become_the_domain_label(raw, expected):
    """A real listing gave the victim's domain as its name.

    URL-stripping consumed the whole thing and left the stub "www", which was
    then reported as a victim.
    """
    assert normalize_name(raw) == expected


@pytest.mark.parametrize("stub", ["www", "http", "com", "  www  "])
def test_url_stubs_are_not_victims(stub):
    assert normalize_name(stub) == ""


def test_stub_names_are_skipped_by_the_scraper():
    html = (
        '<html><body>'
        '<div class="victim-card"><h3>www</h3><p class="description">x</p></div>'
        '<div class="victim-card"><h3>Real Company Ltd</h3><p class="description">y</p></div>'
        "</body></html>"
    )
    assert [i.title for i in scrape(html)] == ["Real Company Ltd"]
