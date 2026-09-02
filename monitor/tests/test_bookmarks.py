"""The curated bookmarks list: parsing, style-preserving writes, health, proposals."""

from __future__ import annotations

import json

import pytest

from intothedarkness.bookmarks import (
    Bookmarks,
    Category,
    Link,
    discover,
    dumps,
    guess_category,
    health,
    save,
)

V3_A = "a" * 56 + ".onion"
V3_B = "b" * 56 + ".onion"
V3_C = "c" * 56 + ".onion"
V2 = "d" * 16 + ".onion"

# Mirrors the real file's authored style: two-space structure, links on one line.
RAW = f"""{{
  "title": "IntoTheDarkness — Tor / Onion Investigation Bookmarks",
  "description": "Curated onion and clearnet links.",
  "categories": [
    {{
      "name": "Ransomware & Extortion Leak Sites",
      "note": "Addresses rotate frequently — verify before relying on any single one.",
      "links": [
        {{ "title": "Alpha", "url": "http://{V3_A}/" }},
        {{ "title": "Beta", "url": "http://{V3_B}/blog" }}
      ]
    }},
    {{
      "name": "OSINT Tools",
      "note": "Clearnet utilities.",
      "links": [
        {{ "title": "Example Tool", "url": "https://example.com/" }}
      ]
    }}
  ]
}}
"""


@pytest.fixture
def book_file(tmp_path):
    path = tmp_path / "bookmarks.json"
    path.write_text(RAW, encoding="utf-8")
    return path


@pytest.fixture
def book(book_file) -> Bookmarks:
    return Bookmarks.load(book_file)


# --------------------------------------------------------------------- parsing


def test_parses_categories_and_links(book):
    assert book.counts() == {"categories": 2, "links": 3, "onion": 2, "clearnet": 1}
    assert book.category("OSINT Tools").links[0].title == "Example Tool"


def test_category_lookup_is_case_insensitive(book):
    assert book.category("osint tools") is not None
    assert book.category("nope") is None


def test_rejects_a_document_without_categories():
    with pytest.raises(ValueError, match="categories"):
        Bookmarks.from_dict({"title": "x"})


def test_links_without_a_url_are_dropped():
    data = {"categories": [{"name": "c", "links": [{"title": "no url"}, {"url": "http://x.com/"}]}]}
    assert len(Bookmarks.from_dict(data).links) == 1


def test_unknown_link_fields_are_preserved(tmp_path):
    """generate.py ignores extra keys, so carrying them through must be lossless."""
    data = {
        "title": "t",
        "description": "d",
        "categories": [
            {"name": "c", "links": [{"title": "A", "url": "http://x.com/", "note": "keep me"}]}
        ],
    }
    book = Bookmarks.from_dict(data)
    assert book.links[0][1].extra == {"note": "keep me"}
    assert '"note": "keep me"' in dumps(book)


# ------------------------------------------------------------------ formatting


def test_round_trip_is_byte_exact(book_file):
    """A rewrite must not reflow the file, or every diff becomes unreviewable."""
    assert dumps(Bookmarks.load(book_file)) == book_file.read_text(encoding="utf-8")


def test_adding_one_link_changes_only_two_lines(book, book_file):
    before = book_file.read_text(encoding="utf-8").splitlines()
    book.add("Ransomware & Extortion Leak Sites", Link("Gamma", f"http://{V3_C}/"))
    after = dumps(book).splitlines()

    added = [line for line in after if line not in before]
    removed = [line for line in before if line not in after]
    # The new entry, plus the previous last entry gaining a trailing comma.
    assert len(added) == 2
    assert len(removed) == 1
    assert any("Gamma" in line for line in added)


def test_written_file_is_still_valid_json(book, tmp_path):
    book.add("OSINT Tools", Link("Another", "https://other.example/"))
    path = tmp_path / "out.json"
    save(book, path)
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert len(reloaded["categories"][1]["links"]) == 2


def test_non_ascii_is_written_literally(book):
    """The real file keeps em-dashes as characters, not \\u escapes."""
    assert "—" in dumps(book)
    assert "\\u2014" not in dumps(book)


def test_expanded_style_is_available_but_not_default(book):
    expanded = dumps(book, compact_links=False)
    assert '"title": "Alpha"' in expanded
    assert "{ \"title\"" not in expanded


# ------------------------------------------------------------------- dedupe


def test_has_matches_on_host_ignoring_scheme_and_path(book):
    assert book.has(f"http://{V3_A}/")
    assert book.has(f"https://{V3_A}/some/other/path")
    assert not book.has(f"http://{V3_C}/")


def test_add_refuses_a_duplicate_host(book):
    assert book.add("Ransomware & Extortion Leak Sites", Link("Dup", f"https://{V3_A}/x")) is False
    assert book.counts()["links"] == 3


def test_add_creates_a_missing_category(book):
    assert book.add("New Category", Link("X", f"http://{V3_C}/"))
    assert book.category("New Category") is not None


def test_guess_category_routes_by_wording():
    assert guess_category("http://x.onion/", "Some Search Engine") == "Directories & Search Engines"
    assert guess_category("http://x.onion/", "Hacker Forum") == "Forums & Communities"
    assert guess_category("http://x.onion/", "Acme Leaks") == "Ransomware & Extortion Leak Sites"


# -------------------------------------------------------------------- health


def test_non_http_schemes_are_skipped_not_reported_dead():
    """`about:manual` and `tonsite://` are deliberate entries, not breakage."""
    for url in ("about:manual", "tonsite://safepay.ton"):
        result = health.check_link(Link("X", url), "cat", fetcher=None)
        assert result.status == health.SKIPPED
        assert "not fetchable" in result.detail


def test_malformed_onion_is_invalid_without_a_fetch():
    result = health.check_link(Link("X", "http://tooshort.onion/"), "cat", fetcher=None)
    assert result.status == health.INVALID
    assert "expected 56" in result.detail


def test_offline_audit_finds_unroutable_addresses():
    book = Bookmarks(
        categories=[
            Category(
                name="c",
                links=[
                    Link("ok", f"http://{V3_A}/"),
                    Link("v2", f"http://{V2}/"),
                    Link("clearnet", "https://example.com/"),
                ],
            )
        ]
    )
    broken = health.stale_v2_or_malformed(book)
    assert [link.title for _, link, _ in broken] == ["v2"]


def test_fetch_failure_is_dead_and_redacted(settings):
    from intothedarkness.scrapers import Fetcher

    settings.tor_socks_url = "socks5://127.0.0.1:1"
    settings.tor_max_retries = 1
    with Fetcher(settings) as fetcher:
        result = health.check_link(Link("X", f"http://{V3_A}/"), "cat", fetcher, settings)

    assert result.status == health.DEAD
    assert V3_A not in result.detail  # onion address stays out of the record


def test_summarize_counts_statuses():
    results = [
        health.Health("a", "u", "c", health.ALIVE),
        health.Health("b", "u", "c", health.DEAD),
        health.Health("c", "u", "c", health.DEAD),
    ]
    assert health.summarize(results) == {health.ALIVE: 1, health.DEAD: 2}
    assert len(health.dead_entries(results)) == 2


# ------------------------------------------------------------------ proposals


def test_proposals_exclude_what_is_already_listed(book):
    proposals = discover.from_urls([f"http://{V3_A}/", f"http://{V3_C}/"], book)
    assert [p.host for p in proposals] == [V3_C]


def test_proposals_reject_v2_and_junk(book):
    junk = "z" * 56 + ".onion"
    urls = [f"http://{V2}/", "not a url", f"http://{junk}/"]
    reasons = discover.rejected(urls, book)
    assert "not a valid v3 onion address" in reasons[f"http://{V2}/"]
    assert "not a usable hostname" in reasons["not a url"]
    assert len(discover.from_urls(urls, book)) == 1


def test_repeated_addresses_collapse_to_one_proposal(book):
    proposals = discover.from_urls([f"http://{V3_C}/", V3_C, f"https://{V3_C}/x"], book)
    assert len(proposals) == 1
    assert proposals[0].seen == 3


def test_proposals_from_findings_rank_by_corroboration(book, repo):
    from intothedarkness.models import Finding, FindingKind, Item

    def finding(target, onion, title):
        return Finding(
            kind=FindingKind.NEW,
            target=target,
            item=Item(
                key=title, target=target, title=title,
                fields={"iocs": {"onion": [onion]}},
            ),
        )

    repo.save_findings([
        finding("t1", V3_C, "Mirror One"),
        finding("t2", V3_C, "Mirror One"),   # a second source corroborates
        finding("t1", "e" * 56 + ".onion", "Other"),
        finding("t1", V3_A, "Already listed"),  # must be filtered out
    ])
    proposals = discover.from_findings(repo.recent_findings(limit=100), book)

    assert [p.host for p in proposals][0] == V3_C  # best corroborated first
    assert V3_A not in [p.host for p in proposals]
    assert proposals[0].sources == {"t1", "t2"}


def test_proposal_converts_to_a_link(book):
    proposal = discover.from_urls([f"http://{V3_C}/"], book)[0]
    link = proposal.to_link()
    assert link.url == f"http://{V3_C}/"
    assert book.add(proposal.suggested_category, link)


def test_valid_host_rules():
    assert discover.valid_host(V3_A)
    assert not discover.valid_host(V2)
    assert discover.valid_host("ransomlook.io")
    assert discover.valid_host("91.215.85.45")
    assert not discover.valid_host("not a url")
    assert not discover.valid_host("")
