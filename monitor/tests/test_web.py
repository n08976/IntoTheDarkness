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
