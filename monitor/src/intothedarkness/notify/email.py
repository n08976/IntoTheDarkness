"""SMTP email delivery."""

from __future__ import annotations

import contextlib
import smtplib
from email.message import EmailMessage

from .base import Message, Notifier, register


@register
class EmailNotifier(Notifier):
    name = "email"

    def available(self) -> tuple[bool, str]:
        s = self.settings
        if not s.smtp_host:
            return False, "ITD_SMTP_HOST is not set"
        if not s.email_from:
            return False, "ITD_EMAIL_FROM is not set"
        if not s.email_to:
            return False, "ITD_EMAIL_TO is not set"
        return True, ""

    def send(self, message: Message) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"email channel unavailable: {why}")

        s = self.settings
        msg = EmailMessage()
        msg["Subject"] = message.subject
        msg["From"] = s.email_from
        msg["To"] = ", ".join(s.email_to)
        msg.set_content(message.text or "(no body)")
        if message.html:
            msg.add_alternative(message.html, subtype="html")

        if s.smtp_ssl:
            server: smtplib.SMTP = smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, timeout=30)
        else:
            server = smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30)

        try:
            server.ehlo()
            if s.smtp_starttls and not s.smtp_ssl:
                try:
                    server.starttls()
                    server.ehlo()
                except smtplib.SMTPNotSupportedError:
                    # Fail closed. Never put credentials or victim names on an
                    # unencrypted connection because the server declined to
                    # upgrade; abort and say why.
                    raise RuntimeError(
                        f"{s.smtp_host}:{s.smtp_port} does not support STARTTLS; "
                        "refusing to send unencrypted. Use port 465 with "
                        "ITD_SMTP_SSL=true, or a host whose certificate is valid."
                    ) from None
            if s.smtp_user:
                server.login(s.smtp_user, s.smtp_password)
            server.send_message(msg)
        finally:
            with contextlib.suppress(smtplib.SMTPException, OSError):
                server.quit()
