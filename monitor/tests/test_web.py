from intothedarkness.notify import get_notifier
from intothedarkness.notify.base import Message


def test_web_channel_writes_a_plain_page_and_a_dated_copy(settings, tmp_path):
    settings.site_dir = tmp_path
    settings.site_push = True                         # no .git here, so nothing to push
    html = '<div><p>IntoTheDarkness · 2026-09-24</p><a href="http://abc.onion/x">leak</a></div>'
    get_notifier("web", settings).send(Message(subject="s", text="t", html=html))

    page = (tmp_path / "index.html").read_text()
    assert "<title>Scan results" in page and "IntoTheDarkness" not in page   # no product name
    assert 'hxxp://abc[.]onion/x' in page and "http://abc.onion" not in page  # public: de-fanged
    dated = list((tmp_path / "reports").glob("????-??-??-????.html"))
    assert len(dated) == 1 and (tmp_path / "reports" / "index.html").exists()


def test_web_channel_is_unavailable_without_a_directory(settings):
    settings.site_dir = None
    ok, why = get_notifier("web", settings).available()
    assert not ok and "ITD_SITE_DIR" in why


def test_web_channel_runs_the_deploy_command_after_a_push(settings, tmp_path):
    import subprocess

    site = tmp_path / "site"
    site.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", str(site)], check=True)
    bare = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "master", str(bare)], check=True)
    subprocess.run(["git", "-C", str(site), "remote", "add", "origin", str(bare)], check=True)
    subprocess.run(["git", "-C", str(site), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(site), "config", "user.name", "t"], check=True)
    marker = tmp_path / "deployed"
    settings.site_dir = site
    settings.site_push = True
    settings.site_deploy_cmd = f"touch {marker}"

    get_notifier("web", settings).send(Message(subject="s", text="t", html="<p>x</p>"))

    assert marker.exists()                                          # deploy ran after the push
    log = subprocess.run(
        ["git", "-C", str(bare), "log", "--oneline"], capture_output=True, text=True
    )
    assert "Report" in log.stdout                                   # and the push happened


def test_page_carries_a_diagnostics_section_and_flags_open_issues(settings, tmp_path):
    from intothedarkness import diag

    settings.site_dir = tmp_path / "site"
    settings.site_dir.mkdir()
    settings.site_push = False
    settings.issues_file = tmp_path / "issues.jsonl"
    diag.record(settings.issues_file, "tor-down", "Tor could not be rebuilt")
    get_notifier("web", settings).send(Message(subject="s", text="t", html="<p>x</p>"))
    page = (settings.site_dir / "index.html").read_text()
    assert 'id="diagnostics"' in page and "1 open issue(s)" in page and "tor-down" in page
