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
    now = datetime(2026, 9, 24, tzinfo=UTC)
    text = render_digest_text(entries, {"a", "b"}, now=now)
    assert text.index("VENDOR VICTIMS") < text.index("NEW SINCE LAST REPORT")
    assert text.count("Beckman Coulter, Inc") == 1                 # once, at the top
    assert "[vendor: Beckman Coulter]" in text and "by metaencryptor" in text
    html = render_digest_html(entries, {"a", "b"}, now=now)
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
    now = datetime(2026, 9, 15, tzinfo=UTC)          # the filing is a week old
    text = render_digest_text(entries, {"c"}, now=now)
    marks = ("VENDOR VICTIMS", "SEC 8-K CYBER FILINGS", "DISCOVERED IN")
    i_v, i_f, i_d = (text.index(k) for k in marks)
    assert i_v < i_f < i_d
    victims = text.split("SEC 8-K CYBER FILINGS", 1)[0]
    assert "Beckman Coulter, Inc" in victims and "BOSTON SCIENTIFIC" not in victims   # not mixed in
    assert "8-K items 1.05 filed 2026-09-08 — Item 1.05" in text
    assert text.count("BOSTON SCIENTIFIC CORP") == 1
    html = render_digest_html(entries, {"c"}, now=now)
    marks = ("Vendor victims", "SEC 8-K cyber filings", "New since")
    h_v, h_f, h_n = (html.index(k) for k in marks)
    assert h_v < h_f < h_n
    assert html.count("BOSTON SCIENTIFIC CORP") == 1 and "filed 2026-09-08" in html


def test_vendor_entries_older_than_the_priority_window_age_out_marked():
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    now = datetime(2026, 9, 29, 12, tzinfo=UTC)

    def f(key, target, title, **fields):
        item = Item(key=key, target=target, title=title, fields=fields)
        return Finding(kind=FindingKind.NEW, target=target, item=item, created_at=now)

    entries = [
        # 4 days old: stays on top
        f("a", "agg-x", "Beckman Coulter, Inc", watchlist="Beckman Coulter",
          sector="healthcare", published="2026-09-25"),
        # 40 days old, and not a carried sector: ages out yet stays listed, marked
        f("b", "agg-x", "Olympus Corporation", watchlist="Olympus Corporation",
          sector="manufacturing", published="2026-08-20"),
        # a filing 6 weeks old ages out of its box the same way
        f("c", "sec-8k", "BOSTON SCIENTIFIC CORP", watchlist="Boston Scientific",
          form="8-K", items="1.05", published="2026-08-15", status="Item 1.05"),
        # an aged vendor entry that is new this run goes in the NEW section
        f("d", "dls-y", "Cytek Biosciences", watchlist="Cytek Biosciences",
          sector="healthcare", published="2026-08-01"),
        f("e", "agg-x", "Textile City", sector="manufacturing", published="2026-09-28"),
    ]
    text = render_digest_text(entries, {"d", "e"}, priority_days=10, now=now)
    top = text.split("NEW SINCE LAST REPORT", 1)[0]
    assert "LAST 10 DAYS" in top and "Beckman Coulter, Inc" in top
    for aged in ("Olympus Corporation", "BOSTON SCIENTIFIC", "Cytek"):
        assert aged not in top
    assert "SEC 8-K CYBER FILINGS" not in text                     # box empty, so absent
    new, running = text.split("NEW SINCE LAST REPORT", 1)[1].split("DISCOVERED IN", 1)
    assert "Cytek Biosciences   [VENDOR: Cytek Biosciences]" in new
    assert "Olympus Corporation   [VENDOR: Olympus Corporation]" in running
    assert "BOSTON SCIENTIFIC CORP   [SEC 8-K: Boston Scientific]" in running
    assert "Textile City" not in running and "plus 0" not in text  # carry rule still applies
    assert text.count("Beckman Coulter, Inc") == 1

    html = render_digest_html(entries, {"d", "e"}, priority_days=10, now=now)
    assert "last 10 days" in html
    assert "SEC 8-K ·\n    vendor: Boston Scientific" in html
    assert "VENDOR ·\n    vendor: Olympus Corporation" in html
    assert html.index("Beckman Coulter, Inc") < html.index("New since last report")
    assert html.index("Olympus Corporation") > html.index("Discovered in the last")

    # 0 keeps the old behaviour: everything vendor-flagged stays on top
    text0 = render_digest_text(entries, {"d", "e"}, priority_days=0, now=now)
    top0 = text0.split("NEW SINCE LAST REPORT", 1)[0]
    assert "Olympus Corporation" in top0 and "BOSTON SCIENTIFIC" in top0


def test_x_feed_posts_render_in_their_own_section_not_the_victim_lists():
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    now = datetime(2026, 10, 3, 14, tzinfo=UTC)

    def f(key, target, title, **fields):
        item = Item(key=key, target=target, title=title, url=fields.pop("url", ""), fields=fields)
        return Finding(kind=FindingKind.NEW, target=target, item=item, created_at=now)

    entries = [
        f("x:1", "x-feeds", "qilin added a hospital to its leak site", source="x",
          handle="vxunderground", sector="healthcare",
          published="Fri Oct 03 13:00:00 +0000 2026", url="https://x.com/vxunderground/status/1"),
        f("x:2", "x-feeds", "Acme Corp breach claimed", source="x", handle="FalconFeedsio",
          watchlist="Acme Corp", published="Fri Oct 03 12:00:00 +0000 2026",
          url="https://x.com/FalconFeedsio/status/2"),
        f("v1", "dls-redact", "Hologic", watchlist="Hologic", sector="healthcare",
          published="2026-10-01"),
        f("n1", "agg-x", "Some Hospital", sector="healthcare", published="2026-10-02"),
    ]
    text = render_digest_text(entries, {"x:1", "x:2", "n1"}, now=now, priority_days=10)
    assert "MONITORED X FEEDS" in text
    assert text.index("MONITORED X FEEDS") < text.index("NEW SINCE LAST REPORT")
    assert "@vxunderground" in text and "@FalconFeedsio" in text
    # the two tweets are in the X section, not the vendor-victims section
    vendor_block = text.split("MONITORED X FEEDS", 1)[0]
    assert "vendor: Hologic" in vendor_block.lower() or "Hologic" in vendor_block
    assert "FalconFeedsio" not in vendor_block        # the watchlist tweet is not a vendor victim

    html = render_digest_html(entries, {"x:1", "x:2", "n1"}, now=now, priority_days=10)
    assert "Monitored X feeds" in html and "@vxunderground" in html
    assert html.index("Monitored X feeds") < html.index("New since last report")
