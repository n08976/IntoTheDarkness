from intothedarkness import diag


def test_issues_open_until_something_later_resolves_them(tmp_path):
    p = tmp_path / "issues.jsonl"
    diag.record(p, "tor-down", "Tor could not be rebuilt")
    assert [e.kind for e in diag.open_issues(diag.load(p))] == ["tor-down"]
    diag.record(p, "tor-recovered", "rebuilt through meek")
    assert diag.open_issues(diag.load(p)) == []
    diag.record(p, "sweep-partial", "one target errored", "dls-x: timeout")
    diag.record(p, "sweep-partial", "one target errored again")
    assert [e.summary for e in diag.open_issues(diag.load(p))] == ["one target errored again"]
    diag.record(p, "sweep-ok", "12 target(s), 840 item(s)")
    events = diag.load(p)
    assert diag.open_issues(events) == [] and diag.last_ok(events).summary.startswith("12 target")


def test_render_shows_open_issues_and_recent_events(tmp_path):
    p = tmp_path / "issues.jsonl"
    diag.record(p, "sweep-failed", "itd run exited 1", "Traceback ...")
    text = diag.render_text(diag.load(p))
    assert "OPEN ISSUES (1)" in text and "sweep-failed" in text and "Traceback" in text
    html = diag.render_html(diag.load(p))
    assert 'id="diagnostics"' in html and "Open issues (1)" in html
    diag.record(p, "sweep-ok", "clean")
    assert "open issues: none" in diag.render_text(diag.load(p))


def test_a_torn_line_does_not_break_loading(tmp_path):
    p = tmp_path / "issues.jsonl"
    diag.record(p, "sweep-ok", "clean")
    p.write_text(p.read_text() + '{"when": "2026-09-24T00:00:00+00:00", "kind": "sw')
    assert [e.kind for e in diag.load(p)] == ["sweep-ok"]
