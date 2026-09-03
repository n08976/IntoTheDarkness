"""Write the message to disk instead of sending it.

The console channel already shows the plain-text body — it is the same renderer
the email uses. What you cannot otherwise see is the HTML part, which is what
actually renders in an inbox. This writes both, so a report can be checked in a
browser before anyone wires up SMTP.
"""

from __future__ import annotations

import webbrowser
from pathlib import Path

from ..config import Settings
from .base import Message, Notifier, register


@register
class PreviewNotifier(Notifier):
    """Save the message as HTML rather than delivering it."""

    name = "preview"

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.last_path: Path | None = None
        self.open_after = False

    def destination(self) -> Path:
        path = self.settings.data_dir / "previews"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def send(self, message: Message) -> None:
        from ..models import utcnow

        stamp = utcnow().strftime("%Y%m%d-%H%M%S")
        target = self.destination() / f"report-{stamp}.html"

        # The subject is not part of the HTML body, so show it as the page title
        # and a header — otherwise the preview omits half of what gets sent.
        page = (
            "<!doctype html><html><head><meta charset='utf-8'>"
            f"<title>{_escape(message.subject)}</title></head>"
            "<body style='margin:0;background:#f3f4f6;padding:24px'>"
            "<div style='max-width:720px;margin:0 auto;background:#fff;"
            "border:1px solid #e5e7eb;border-radius:6px;overflow:hidden'>"
            "<div style='padding:12px 16px;border-bottom:1px solid #e5e7eb;"
            "background:#f9fafb;font-family:system-ui,sans-serif;font-size:13px'>"
            f"<div style='color:#6b7280'>Subject</div>"
            f"<div style='font-weight:600'>{_escape(message.subject)}</div></div>"
            f"<div style='padding:16px'>{message.html or _pre(message.text)}</div>"
            "</div></body></html>"
        )
        target.write_text(page, encoding="utf-8")

        text_path = target.with_suffix(".txt")
        text_path.write_text(
            f"Subject: {message.subject}\n\n{message.text}\n", encoding="utf-8"
        )

        self.last_path = target
        if self.open_after:
            webbrowser.open(target.as_uri())


def _escape(text: str) -> str:
    from html import escape

    return escape(text or "")


def _pre(text: str) -> str:
    style = "white-space:pre-wrap;font-family:ui-monospace,monospace"
    return f"<pre style='{style}'>{_escape(text)}</pre>"
