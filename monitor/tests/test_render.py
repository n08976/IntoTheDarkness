from __future__ import annotations

from datetime import UTC, datetime

from intothedarkness.models import Finding, FindingKind, Item, Severity
from intothedarkness.notify import (
    render_digest_html,
    render_digest_text,
    render_html,
    render_subject,
    render_text,
)


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

    now = datetime(2026, 9, 12, tzinfo=UTC)
    entries = [
        f("a", "Older Clinic", "healthcare", "2026-08-01"),
        f("b", "Mid Widgets", "manufacturing", "2026-08-15"),
        f("c", "Newest Hospital", "healthcare", "2026-09-10"),
    ]
    # window wide enough to keep all three; carry=() lists every sector
    text = render_digest_text(entries, set(), carry=(), now=now, window_days=90)
    # Newest is in Recent, the two older ones in Previous below it: still newest-first overall
    assert text.index("Newest Hospital") < text.index("Mid Widgets") < text.index("Older Clinic")
    assert "-- healthcare" not in text                     # no sector headings
    assert "[manufacturing] Mid Widgets" in text           # sector inline instead

    html = render_digest_html(entries, set(), carry=(), now=now, window_days=90)
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
        item = Item(key=key, target="t", title=title, fields={"sector": sector}, seen_at=when)
        return Finding(kind=FindingKind.NEW, target="t", created_at=when, item=item)

    entries = [f("a", "Acme Steel", "manufacturing", 0), f("b", "Mercy Clinic", "healthcare", 1),
               f("c", "Old Mill", "manufacturing", 10), f("d", "Old Hospital", "healthcare", 12)]
    now = datetime(2026, 9, 23, tzinfo=UTC)
    text = render_digest_text(entries, {"a", "b"}, now=now)
    new = text.split("NEW SINCE LAST REPORT", 1)[1].split("PREVIOUS", 1)[0]
    assert new.index("Mercy Clinic") < new.index("Acme Steel")   # priority first though older
    previous = text.split("PREVIOUS", 1)[1]
    assert "Old Hospital" in previous and "Old Mill" not in previous     # carried sectors only
    assert "plus 1 in other sectors" in previous
    html = render_digest_html(entries, {"a", "b"}, now=now)
    assert html.index("Mercy Clinic") < html.index("Acme Steel") and "Old Mill" not in html
    everything = render_digest_text(entries, {"a", "b"}, carry=(), now=now)
    assert "Old Mill" in everything.split("PREVIOUS", 1)[1]


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
    marks = ("VENDOR VICTIMS", "SEC 8-K CYBER FILINGS", "NEW SINCE LAST REPORT")
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
    text = render_digest_text(entries, {"d", "e"}, priority_days=10, now=now, window_days=90)
    top = text.split("NEW SINCE LAST REPORT", 1)[0]
    assert "LAST 10 DAYS" in top and "Beckman Coulter, Inc" in top
    for aged in ("Olympus Corporation", "BOSTON SCIENTIFIC", "Cytek"):
        assert aged not in top
    assert "SEC 8-K CYBER FILINGS" not in text                     # box empty, so absent
    new, previous = text.split("NEW SINCE LAST REPORT", 1)[1].split("PREVIOUS", 1)
    assert "Cytek Biosciences   [VENDOR: Cytek Biosciences]" in new
    assert "Olympus Corporation   [VENDOR: Olympus Corporation]" in previous
    assert "BOSTON SCIENTIFIC CORP   [SEC 8-K: Boston Scientific]" in previous
    assert "Textile City" not in previous and "plus 0" not in text  # carry rule still applies
    assert text.count("Beckman Coulter, Inc") == 1

    html = render_digest_html(entries, {"d", "e"}, priority_days=10, now=now, window_days=90)
    assert "last 10 days" in html
    assert html.index("Beckman Coulter, Inc") < html.index("New since last report")
    # aged vendor/8-K entries drop into the compact Previous list, still marked
    prev_html = html.split("Previous", 1)[1]
    assert "Olympus Corporation" in prev_html and "VENDOR" in prev_html
    assert "Boston Scientific" in prev_html and "SEC 8-K" in prev_html

    # 0 keeps the old behaviour: everything vendor-flagged stays on top
    text0 = render_digest_text(entries, {"d", "e"}, priority_days=0, now=now, window_days=90)
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
    # Social intel is the least confirmed section, so it comes last.
    assert text.index("MONITORED X FEEDS") > text.index("NEW SINCE LAST REPORT")
    # the healthcare tweet stays in the feeds section
    feeds = text.split("MONITORED X FEEDS", 1)[1]
    assert "@vxunderground" in feeds
    # the watchlist tweet is promoted to Vendor Victims (marked via X), not the feeds box
    vendor_block = text.split("MONITORED X FEEDS", 1)[0]
    assert "[vendor: Hologic]" in vendor_block
    assert "@FalconFeedsio" in vendor_block and "Acme Corp breach" in vendor_block
    assert "FalconFeedsio" not in feeds

    html = render_digest_html(entries, {"x:1", "x:2", "n1"}, now=now, priority_days=10)
    assert "Monitored X feeds" in html and "@vxunderground" in html
    assert html.index("Monitored X feeds") > html.index("New since last report")


def test_x_post_naming_a_vendor_goes_to_vendor_victims_marked_via_x():
    from datetime import UTC, datetime

    from intothedarkness.models import Finding, FindingKind, Item
    from intothedarkness.notify import render_digest_html, render_digest_text

    now = datetime(2026, 10, 9, 14, tzinfo=UTC)

    def f(key, title, **fields):
        item = Item(key=key, target="x-feeds", title=title,
                    url=fields.pop("url", ""), fields=fields)
        return Finding(kind=FindingKind.NEW, target="x-feeds", item=item, created_at=now)

    entries = [
        # an X post that matched the watchlist -> Vendor Victims, not the feeds box
        f("x:1", "Acme Corp breach dumped by a ransomware crew", source="x",
          handle="FalconFeedsio", watchlist="Acme Corp",
          published="Thu Oct 09 10:00:00 +0000 2026", url="https://x.com/FalconFeedsio/status/1"),
        # a plain healthcare X post -> stays in the feeds section
        f("x:2", "a regional hospital reports a cyber incident", source="x",
          handle="vxunderground", sector="healthcare",
          published="Thu Oct 09 09:00:00 +0000 2026", url="https://x.com/vxunderground/status/2"),
    ]
    text = render_digest_text(entries, {"x:1", "x:2"}, now=now)
    vendors = text.split("MONITORED X FEEDS", 1)[0] if "MONITORED X FEEDS" in text else text
    assert "VENDOR VICTIMS" in text
    assert "Acme Corp breach" in vendors and "(via X: @FalconFeedsio)" in vendors
    # the vendor hit is NOT duplicated in the feeds section; the hospital post is
    feeds = text.split("MONITORED X FEEDS", 1)[1]
    assert "regional hospital" in feeds and "Acme Corp breach" not in feeds

    html = render_digest_html(entries, {"x:1", "x:2"}, now=now)
    assert "Vendor victims" in html and "via X · @FalconFeedsio" in html
    assert html.index("Acme Corp breach") < html.index("Monitored X feeds")


def test_report_keeps_what_is_current_and_the_archive_takes_the_rest():
    from intothedarkness.notify.render import bucket, render_archive_html

    now = datetime(2026, 11, 20, 12, tzinfo=UTC)

    def f(key, target, title, **fields):
        fields.setdefault("sector", "healthcare")
        item = Item(target=target, key=key, title=title, fields=fields)
        return Finding(target=target, kind=FindingKind.NEW, severity=Severity.HIGH,
                       item=item, message=title, created_at=now)

    def x(key, title, published):
        return f(key, "x", title, source="x", handle="feed", published=published)

    entries = [
        f("fresh", "dls", "Fresh Clinic", published="2026-11-18"),
        f("week", "dls", "Week Old Clinic", published="2026-11-13"),
        f("stale", "dls", "Stale Hospital", published="2026-10-20"),
        f("other", "dls", "Stale Factory", sector="manufacturing", published="2026-10-20"),
        f("vendor", "dls", "Old Vendor Inc", sector="technology", published="2026-10-25",
          watchlist="Old Vendor"),
        f("ancient", "dls", "Ancient Hospital", published="2026-01-05"),
        x("x:new", "Post today", "2026-11-20T08:00:00+00:00"),
        x("x:old", "Post last week", "2026-11-14T08:00:00+00:00"),
        x("x:late", "Old post found today", "2026-11-01T08:00:00+00:00"),
    ]
    shape = dict(window_days=10, recent_days=4, priority_days=10, social_days=3, now=now)

    b = bucket(entries, {"x:late"}, **shape)
    assert [g.item.key for g in b.recent] == ["fresh"]
    assert [g.item.key for g in b.previous] == ["week"]
    # New to the reader today, so shown once however old its source says it is.
    assert {g.item.key for g in b.social} == {"x:new", "x:late"}
    # Aged out: carried sector, a watchlist vendor of any sector, and old posts.
    assert {g.item.key for g in b.archive} == {"stale", "vendor", "ancient", "x:old"}
    assert b.archive_rest == 1                                    # the factory: counted only

    text = render_digest_text(entries, {"x:late"}, archive_url="https://s.example/archive.html",
                              **shape)
    assert "Stale Hospital" not in text and "Post last week" not in text
    assert "4 older entries are in the archive: https://s.example/archive.html" in text
    html = render_digest_html(entries, {"x:late"}, archive_url="archive.html", **shape)
    assert "Stale Hospital" not in html and 'href="archive.html"' in html

    page = render_archive_html(entries, {"x:late"}, archive_days=180, **shape)
    assert "October 2026 (2)" in page and "November 2026 (1)" in page
    assert "Old Vendor Inc" in page and "VENDOR" in page          # still says what it was
    assert "Fresh Clinic" not in page and "Week Old Clinic" not in page   # never in both
    assert "Ancient Hospital" not in page                         # past the archive horizon
    assert "Stale Factory" not in page and "1 in other sectors" in page


def test_mail_caps_the_x_section_and_says_how_many_it_left_out():
    now = datetime(2026, 11, 20, 12, tzinfo=UTC)
    entries = [
        Finding(target="x", kind=FindingKind.NEW, severity=Severity.LOW, message="p",
                created_at=now,
                item=Item(target="x", key=f"x:{i}", title=f"Post {i:02d}",
                          fields={"source": "x", "handle": "feed", "sector": "healthcare",
                                  "published": f"2026-11-20T{i:02d}:00:00+00:00"}))
        for i in range(8)
    ]
    text = render_digest_text(entries, set(), social_days=3, social_max=5, now=now)
    assert "Post 07" in text and "Post 03" in text and "Post 02" not in text   # newest kept
    assert "plus 3 more, on the website" in text
    assert "Post 02" in render_digest_text(entries, set(), social_days=3, now=now)   # no cap
