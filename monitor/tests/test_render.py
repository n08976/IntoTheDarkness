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
