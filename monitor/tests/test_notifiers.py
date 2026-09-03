"""Email and webhook delivery, without touching a real server."""

from __future__ import annotations

import httpx
import pytest
import respx

from intothedarkness.models import Finding, FindingKind, Item, Severity
from intothedarkness.notify import Message, get_notifier, render_html, render_subject, render_text


class FakeSMTP:
    """Records what would have been sent."""

    instances: list[FakeSMTP] = []

    def __init__(self, host, port, timeout=None):
        self.host, self.port = host, port
        self.started_tls = False
        self.login_args = None
        self.messages = []
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def ehlo(self): ...
    def starttls(self): self.started_tls = True
    def login(self, user, password): self.login_args = (user, password)
    def send_message(self, msg): self.messages.append(msg)
    def quit(self): self.quit_called = True


@pytest.fixture(autouse=True)
def reset_smtp():
    FakeSMTP.instances = []


@pytest.fixture
def mail_settings(settings):
    settings.smtp_host = "smtp.example.com"
    settings.smtp_port = 587
    settings.smtp_user = "bot"
    settings.smtp_password = "secret"
    settings.email_from = "itd@example.com"
    settings.email_to = ["you@example.com", "them@example.com"]
    return settings


def sample_message() -> Message:
    findings = [
        Finding(
            kind=FindingKind.NEW,
            target="t",
            severity=Severity.HIGH,
            item=Item(key="k", target="t", title="Alpha", url="https://e.com/1"),
        )
    ]
    return Message(
        subject=render_subject(findings),
        text=render_text(findings),
        html=render_html(findings),
        findings=findings,
    )


def test_email_reports_what_is_missing(settings):
    notifier = get_notifier("email", settings)
    ok, why = notifier.available()
    assert not ok and "SMTP_HOST" in why

    settings.smtp_host = "smtp.example.com"
    assert "EMAIL_FROM" in notifier.available()[1]

    settings.email_from = "a@b.c"
    assert "EMAIL_TO" in notifier.available()[1]


def test_email_refuses_to_send_when_unconfigured(settings):
    with pytest.raises(RuntimeError, match="unavailable"):
        get_notifier("email", settings).send(sample_message())


def test_email_sends_multipart_with_starttls_and_login(mail_settings, monkeypatch):
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    get_notifier("email", mail_settings).send(sample_message())

    server = FakeSMTP.instances[0]
    assert (server.host, server.port) == ("smtp.example.com", 587)
    assert server.started_tls
    assert server.login_args == ("bot", "secret")
    assert server.quit_called

    msg = server.messages[0]
    assert msg["To"] == "you@example.com, them@example.com"
    assert msg["From"] == "itd@example.com"
    assert "HIGH" in msg["Subject"]

    bodies = {part.get_content_type(): part.get_content() for part in msg.walk()
              if part.get_content_type().startswith("text/")}
    assert "Alpha" in bodies["text/plain"]
    assert "https://e.com/1" in bodies["text/html"]


def test_email_uses_ssl_transport_when_asked(mail_settings, monkeypatch):
    mail_settings.smtp_ssl = True
    monkeypatch.setattr("smtplib.SMTP_SSL", FakeSMTP)
    get_notifier("email", mail_settings).send(sample_message())
    # With implicit SSL there is no STARTTLS upgrade.
    assert FakeSMTP.instances[0].started_tls is False


def test_email_skips_login_when_no_user_is_set(mail_settings, monkeypatch):
    mail_settings.smtp_user = ""
    monkeypatch.setattr("smtplib.SMTP", FakeSMTP)
    get_notifier("email", mail_settings).send(sample_message())
    assert FakeSMTP.instances[0].login_args is None


def test_email_quits_the_server_even_when_sending_fails(mail_settings, monkeypatch):
    class Exploding(FakeSMTP):
        def send_message(self, msg):
            raise RuntimeError("relay refused")

    monkeypatch.setattr("smtplib.SMTP", Exploding)
    with pytest.raises(RuntimeError, match="relay refused"):
        get_notifier("email", mail_settings).send(sample_message())
    assert FakeSMTP.instances[0].quit_called


@respx.mock
def test_webhook_posts_json(settings):
    settings.webhook_url = "https://hooks.example.com/x"
    route = respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(200))

    get_notifier("webhook", settings).send(sample_message())

    import json

    body = json.loads(route.calls[0].request.read())
    assert body["severity"] == "high"
    assert "Alpha" in body["text"]
    assert body["findings"][0]["item"]["url"] == "https://e.com/1"


@respx.mock
def test_webhook_raises_on_http_error(settings):
    settings.webhook_url = "https://hooks.example.com/x"
    respx.post("https://hooks.example.com/x").mock(return_value=httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        get_notifier("webhook", settings).send(sample_message())


def test_webhook_reports_missing_configuration(settings):
    ok, why = get_notifier("webhook", settings).available()
    assert not ok and "WEBHOOK_URL" in why


# -------------------------------------------------------------------- preview


def test_preview_writes_html_and_text_without_sending(settings):
    settings.ensure_dirs()
    notifier = get_notifier("preview", settings)
    notifier.send(sample_message())

    assert notifier.last_path is not None and notifier.last_path.exists()
    html = notifier.last_path.read_text(encoding="utf-8")
    assert "Alpha" in html
    assert "<!doctype html>" in html.lower()
    # The subject is not part of the email body, so the preview must add it or
    # it shows only half of what would be sent.
    assert "HIGH" in html

    text = notifier.last_path.with_suffix(".txt").read_text(encoding="utf-8")
    assert text.startswith("Subject:")
    assert "Alpha" in text


def test_preview_escapes_scraped_content_in_the_subject(settings):
    from intothedarkness.notify import Message

    settings.ensure_dirs()
    notifier = get_notifier("preview", settings)
    notifier.send(Message(subject="<script>alert(1)</script>", text="body", findings=[]))
    html = notifier.last_path.read_text(encoding="utf-8")
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
