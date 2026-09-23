from __future__ import annotations

from intothedarkness.models import Finding, FindingKind, Item, Severity
from intothedarkness.notify import render_html, render_subject, render_text


def finding(target="t", severity=Severity.INFO, title="Something", **kw) -> Finding:
    return Finding(
        kind=FindingKind.NEW,
        target=target,
        severity=severity,
        item=Item(key="k", target=target, title=title, url="https://e.com/1"),
        **kw,
    )


def test_subject_reflects_the_worst_severity_and_scope():
    subject = render_subject([finding(severity=Severity.LOW), finding(severity=Severity.HIGH)])
    assert "HIGH" in subject and "2 findings" in subject

    multi = render_subject([finding(target="a"), finding(target="b")])
    assert "2 targets" in multi


def test_text_groups_by_target():
    text = render_text([finding(target="b"), finding(target="a")])
    assert "== a" in text and "== b" in text


def test_html_escapes_untrusted_scraped_content():
    html = render_html([finding(title="<script>alert(1)</script>")])
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_html_links_items_and_shows_severity_colour():
    html = render_html([finding(severity=Severity.CRITICAL)])
    assert 'href="https://e.com/1"' in html
    assert "#7f1d1d" in html


def test_empty_findings_render_safely():
    assert render_subject([]) == "IntoTheDarkness: no findings"
    assert render_text([]) == "no findings"


def test_digest_html_entries_are_real_markup_not_escaped_text():
    # The entry sub-template is rendered to a string and inserted into an
    # autoescaping outer template. Without marking it safe, every <li> arrived
    # in the inbox as literal "&lt;li ...&gt;" text -- which is what happened.
    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html

    f = Finding(kind=FindingKind.NEW, target="t",
                item=Item(key="k", target="t", title="Acme Hospital", url="https://e.com/1"))
    html = render_digest_html([f], {"k"})
    assert "<li " in html and "&lt;li" not in html
    assert "<strong>Acme Hospital</strong>" in html


def test_running_list_is_one_flat_list_newest_first_with_sector_inline():
    # Sector headings split the timeline; the reader asked for a timeline.
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    def f(key, title, sector, discovered):
        fields = {"sector": sector, "discovered": discovered}
        item = Item(key=key, target="t", title=title, fields=fields)
        return Finding(kind=FindingKind.NEW, target="t", item=item,
                       created_at=datetime(2026, 9, 12, tzinfo=UTC))

    entries = [
        f("a", "Older Clinic", "healthcare", "2026-08-01"),
        f("b", "Mid Widgets", "manufacturing", "2026-08-15"),
        f("c", "Newest Hospital", "healthcare", "2026-09-10"),
    ]
    text = render_digest_text(entries, set(), carry=())      # every sector in the running list
    body = text.split("DISCOVERED IN THE LAST", 1)[1]
    assert body.index("Newest Hospital") < body.index("Mid Widgets") < body.index("Older Clinic")
    assert "-- healthcare" not in text                     # no sector headings
    assert "[manufacturing] Mid Widgets" in text           # sector inline instead

    html = render_digest_html(entries, set(), carry=())
    assert html.index("Newest Hospital") < html.index("Mid Widgets") < html.index("Older Clinic")


def test_vendor_victims_lead_the_report_and_are_not_repeated_below():
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    def f(key, title, **fields):
        when = datetime(2026, 9, 23, tzinfo=UTC)
        item = Item(key=key, target="t", title=title, fields=fields)
        return Finding(kind=FindingKind.NEW, target="t", created_at=when, item=item)

    entries = [
        f("a", "Textile City", sector="manufacturing"),
        f("b", "Beckman Coulter, Inc", sector="healthcare",
          watchlist="Beckman Coulter", group="metaencryptor"),
    ]
    text = render_digest_text(entries, {"a", "b"})
    assert text.index("VENDOR VICTIMS") < text.index("NEW SINCE LAST REPORT")
    assert text.count("Beckman Coulter, Inc") == 1                 # once, at the top
    assert "[vendor: Beckman Coulter]" in text and "by metaencryptor" in text
    html = render_digest_html(entries, {"a", "b"})
    assert html.index("Vendor victims") < html.index("New since last report")
    assert html.count("Beckman Coulter, Inc") == 1


def test_new_section_leads_with_priority_sectors_and_running_list_carries_only_them():
    from datetime import UTC, datetime, timedelta

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    def f(key, title, sector, age):
        when = datetime(2026, 9, 23, tzinfo=UTC) - timedelta(days=age)
        item = Item(key=key, target="t", title=title, fields={"sector": sector})
        return Finding(kind=FindingKind.NEW, target="t", created_at=when, item=item)

    entries = [f("a", "Acme Steel", "manufacturing", 0), f("b", "Mercy Clinic", "healthcare", 1),
               f("c", "Old Mill", "manufacturing", 10), f("d", "Old Hospital", "healthcare", 12)]
    text = render_digest_text(entries, {"a", "b"})
    new = text.split("NEW SINCE LAST REPORT", 1)[1].split("DISCOVERED IN", 1)[0]
    assert new.index("Mercy Clinic") < new.index("Acme Steel")   # priority first though older
    running = text.split("DISCOVERED IN", 1)[1]
    assert "Old Hospital" in running and "Old Mill" not in running       # carried sectors only
    assert "plus 1 entries in other sectors" in running
    html = render_digest_html(entries, {"a", "b"})
    assert html.index("Mercy Clinic") < html.index("Acme Steel") and "Old Mill" not in html
    everything = render_digest_text(entries, {"a", "b"}, carry=())
    assert "Old Mill" in everything.split("DISCOVERED IN", 1)[1]


def test_vendor_sec_filings_get_their_own_section_beneath_vendor_victims():
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    def f(key, target, title, **fields):
        item = Item(key=key, target=target, title=title, fields=fields)
        return Finding(kind=FindingKind.NEW, target=target, item=item,
                       created_at=datetime(2026, 9, 23, tzinfo=UTC))

    entries = [
        f("a", "agg-x", "Beckman Coulter, Inc", watchlist="Beckman Coulter", group="metaencryptor"),
        f("b", "sec-8k", "BOSTON SCIENTIFIC CORP", watchlist="Boston Scientific", form="8-K",
          items="1.05", published="2026-09-08", status="Item 1.05"),
        f("c", "agg-x", "Textile City", sector="manufacturing"),
    ]
    text = render_digest_text(entries, {"c"})
    marks = ("VENDOR VICTIMS", "SEC 8-K CYBER FILINGS", "DISCOVERED IN")
    i_v, i_f, i_d = (text.index(k) for k in marks)
    assert i_v < i_f < i_d
    victims = text.split("SEC 8-K CYBER FILINGS", 1)[0]
    assert "Beckman Coulter, Inc" in victims and "BOSTON SCIENTIFIC" not in victims   # not mixed in
    assert "8-K items 1.05 filed 2026-09-08 — Item 1.05" in text
    assert text.count("BOSTON SCIENTIFIC CORP") == 1
    html = render_digest_html(entries, {"c"})
    marks = ("Vendor victims", "SEC 8-K cyber filings", "New since")
    h_v, h_f, h_n = (html.index(k) for k in marks)
    assert h_v < h_f < h_n
    assert html.count("BOSTON SCIENTIFIC CORP") == 1 and "filed 2026-09-08" in html
