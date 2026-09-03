"""Scraped content must never be able to style or blank our own output."""

from __future__ import annotations

from intothedarkness.models import Finding, FindingKind, Item, Severity
from intothedarkness.notify import Message, get_notifier, render_text


def hostile_finding() -> Finding:
    return Finding(
        kind=FindingKind.NEW,
        target="t",
        severity=Severity.HIGH,
        item=Item(
            key="k",
            target="t",
            title="[red]not a style[/red] and [bold]nor is this",
            url="https://e.com/1",
        ),
    )


def test_console_notifier_does_not_interpret_scraped_markup(settings, capsys):
    findings = [hostile_finding()]
    notifier = get_notifier("console", settings)
    notifier.send(
        Message(subject="[blink]subject", text=render_text(findings), findings=findings)
    )

    out = capsys.readouterr().out
    # The literal brackets survive rather than being consumed as style tags.
    assert "[red]not a style[/red]" in out
    assert "[blink]subject" in out


def test_severity_tag_survives_rendering(settings, capsys):
    findings = [hostile_finding()]
    get_notifier("console", settings).send(
        Message(subject="s", text=render_text(findings), findings=findings)
    )
    # "[high]" is a plausible Rich tag; it must still reach the reader.
    assert "[high]" in capsys.readouterr().out


def test_findings_table_escapes_scraped_messages(repo, monkeypatch, capsys):
    import intothedarkness.cli as cli

    repo.save_findings([hostile_finding()])
    monkeypatch.setattr(cli, "_repo", lambda: repo)
    cli.findings(
        since_hours=24, target=None, min_severity=None, sector=None, kind=None, limit=10
    )

    # The table wraps, so compare with whitespace removed. Both bracket forms
    # survive as literal text instead of being consumed as Rich style tags.
    out = "".join(capsys.readouterr().out.split())
    assert "[/red]" in out
    assert "[bold]" in out


def test_sector_filter_is_not_truncated_by_the_limit(repo, monkeypatch, capsys):
    """Regression: filters ran after the database limit.

    `--sector healthcare --limit 8` meant "healthcare among the 8 newest"
    rather than "the 8 newest healthcare", so it silently under-reported.
    """
    import intothedarkness.cli as cli
    from intothedarkness.models import Finding, FindingKind, Item, Severity

    def victim(name, sector):
        return Finding(
            kind=FindingKind.BASELINE,
            target="agg",
            severity=Severity.HIGH,
            item=Item(key=name, target="agg", title=name, fields={"sector": sector}),
        )

    # 40 noise findings recorded after the ones we care about.
    repo.save_findings([victim(f"Clinic {i}", "healthcare") for i in range(5)])
    repo.save_findings([victim(f"Widget {i}", "manufacturing") for i in range(40)])
    monkeypatch.setattr(cli, "_repo", lambda: repo)

    cli.findings(
        since_hours=24, target=None, min_severity=None,
        sector="healthcare", kind=None, limit=10,
    )
    out = capsys.readouterr().out
    assert out.count("healthcare") >= 5
    assert "manufacturing" not in out


def test_limit_still_caps_filtered_results(repo, monkeypatch, capsys):
    import intothedarkness.cli as cli
    from intothedarkness.models import Finding, FindingKind, Item, Severity

    repo.save_findings([
        Finding(
            kind=FindingKind.NEW, target="agg", severity=Severity.HIGH,
            item=Item(key=f"c{i}", target="agg", title=f"Clinic {i}",
                      fields={"sector": "healthcare"}),
        )
        for i in range(20)
    ])
    monkeypatch.setattr(cli, "_repo", lambda: repo)
    cli.findings(
        since_hours=24, target=None, min_severity=None,
        sector="healthcare", kind=None, limit=3,
    )
    assert capsys.readouterr().out.count("healthcare") == 3
