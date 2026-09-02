"""Selector suggestion for listing pages."""

from __future__ import annotations

from intothedarkness.scrapers.suggest import suggest


def cards(n=6):
    rows = "".join(
        f'<div class="post-card"><h3 class="company">Org {i} Ltd</h3>'
        f'<p class="desc">{i} GB of data</p></div>'
        for i in range(1, n + 1)
    )
    return f"<html><body><div class='wrap'>{rows}</div></body></html>"


def table(n=12):
    rows = "".join(
        f'<tr class="leak"><td class="victim">Company {i} Ltd</td>'
        f'<td class="size">{i} GB</td></tr>'
        for i in range(1, n + 1)
    )
    return f"<html><body><table>{rows}</table></body></html>"


def bare_list(n=9):
    rows = "".join(f'<li><a href="/v/{i}">Victim Org {i}</a></li>' for i in range(1, n + 1))
    return f"<html><body><ul>{rows}</ul></body></html>"


def test_card_layout_finds_container_and_title():
    top = suggest(cards())[0]
    assert top.item == "div.post-card"
    assert top.title == "h3.company"
    assert top.samples[0] == "Org 1 Ltd"


def test_table_layout():
    top = suggest(table())[0]
    assert top.item == "tr.leak"
    assert top.title == "td.victim"


def test_bare_list_layout():
    top = suggest(bare_list())[0]
    assert top.item == "li"
    assert top.title == "a"


def test_navigation_rows_do_not_win_over_the_listing():
    """A nav row is also a <tr>; structural uniformity should demote it."""
    html = (
        "<html><body><table>"
        "<tr><td>Home</td></tr><tr><td>About</td></tr><tr><td>Contact</td></tr>"
        + "".join(
            f'<tr class="leak"><td class="victim">Org {i} Ltd</td>'
            f'<td class="sz">{i} GB</td></tr>'
            for i in range(1, 15)
        )
        + "</table></body></html>"
    )
    top = suggest(html)[0]
    assert top.item == "tr.leak"
    assert top.title == "td.victim"
    assert "Home" not in " ".join(top.samples)


def test_scripts_and_styles_are_ignored():
    html = cards().replace("<div class='wrap'>", "<script>var x=1</script><div class='wrap'>")
    assert suggest(html)[0].item == "div.post-card"


def test_too_few_repeats_are_not_a_listing():
    html = "<html><body><div class='one'>Only</div><div class='one'>Two</div></body></html>"
    assert all(s.item != "div.one" for s in suggest(html))


def test_no_repeating_structure_yields_nothing():
    assert suggest("<html><body><p>just a paragraph</p></body></html>") == []


def test_empty_document_is_handled():
    assert suggest("") == []


def test_suggestion_renders_pasteable_yaml():
    yaml_text = suggest(cards())[0].as_yaml()
    assert 'item: "div.post-card"' in yaml_text
    assert 'title: "h3.company"' in yaml_text


def test_suggestions_are_ranked_and_capped():
    results = suggest(cards(), limit=2)
    assert len(results) <= 2
    assert results == sorted(results, key=lambda s: s.score, reverse=True)


def test_suggested_selectors_actually_work_with_the_dls_scraper():
    """The whole point: paste the suggestion and it extracts the right names."""
    from test_scrapers import FakeFetcher

    from intothedarkness.enrich import SectorClassifier
    from intothedarkness.models import Selectors, Target
    from intothedarkness.scrapers import get_scraper

    top = suggest(cards())[0]
    target = Target(
        name="t",
        url="http://x.onion/",
        scraper="dls",
        selectors=Selectors(item=top.item, title=top.title),
    )
    scraper = get_scraper("dls", FakeFetcher(cards(), url="http://x.onion/"))
    scraper.classifier = SectorClassifier()
    items = scraper.scrape(target)

    assert [i.title for i in items] == [f"Org {i} Ltd" for i in range(1, 7)]
