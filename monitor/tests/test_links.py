from datetime import UTC, datetime, timedelta

from intothedarkness.models import Finding, FindingKind, Item
from intothedarkness.notify import render_digest_html, render_digest_text
from intothedarkness.notify.links import is_weak, merge_links


def finding(target, url="", when=0, **fields):
    item = Item(key=f"{target}-k", target=target, title="Fresenius Medical Care", url=url,
                fields=fields)
    return Finding(kind=FindingKind.NEW, target=target, item=item,
                   created_at=datetime(2026, 9, 23, 10, 0, tzinfo=UTC) + timedelta(seconds=when))


def test_a_site_root_is_a_weak_link_and_a_page_is_not():
    assert is_weak("") and is_weak("https://www.ransomlook.io/")
    assert not is_weak("https://darkfield.orizon.one/victims/86a78542")


def test_merge_keeps_the_first_sighting_but_takes_the_best_links():
    # Measured: the first sighting had no link at all, the second a bare site
    # root, the third and fourth real dossier pages -- and the report showed
    # only the first. It must show the best link and remember the others.
    first = finding("agg-ransomware-live", "", 0, group="shinyhunters", country="DE")
    root = finding("agg-ransomlook", "https://www.ransomlook.io/", 1)
    dossier = finding("agg-darkfield", "https://darkfield.orizon.one/victims/86a7", 2, website="")
    other = finding("agg-ctiwatch", "https://ctiwatch.com/victims/a99c", 3)
    for later in (root, dossier, other):
        merge_links(first, later)
    it = first.item
    assert it.url == "https://darkfield.orizon.one/victims/86a7"          # promoted over the root
    assert [e["source"] for e in it.fields["also"]] == ["ctiwatch"]   # root not worth keeping
    assert first.created_at.second == 0                                     # date untouched


def test_merge_fills_a_missing_website_and_never_overwrites_one():
    a = finding("agg-x", "https://x/1", 0, website="")
    merge_links(a, finding("agg-y", "https://y/2", 1, website="https://fresenius.example"))
    merge_links(a, finding("agg-z", "https://z/3", 2, website="https://other.example"))
    assert a.item.fields["website"] == "https://fresenius.example"


def test_extra_links_reach_both_renderings():
    f = finding("agg-darkfield", "https://darkfield.orizon.one/victims/86a7", 0,
                also=[{"source": "ctiwatch", "url": "https://ctiwatch.com/victims/a99c"}])
    text = render_digest_text([f], {f.item.key})
    html = render_digest_html([f], {f.item.key})
    assert "also:    ctiwatch  https://ctiwatch.com/victims/a99c" in text
    assert 'href="https://ctiwatch.com/victims/a99c"' in html and ">ctiwatch</a>" in html
