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
    text = render_digest_text(entries, set())
    body = text.split("DISCOVERED IN THE LAST", 1)[1]
    assert body.index("Newest Hospital") < body.index("Mid Widgets") < body.index("Older Clinic")
    assert "-- healthcare" not in text                     # no sector headings
    assert "[manufacturing] Mid Widgets" in text           # sector inline instead

    html = render_digest_html(entries, set())
    assert html.index("Newest Hospital") < html.index("Mid Widgets") < html.index("Older Clinic")
