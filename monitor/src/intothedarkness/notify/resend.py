"""Resend HTTP API delivery.

Matches the transport pattern used across this operator's other projects
(`RESEND_API_KEY` + `EMAIL_FROM`): an HTTPS API call rather than SMTP, which
avoids the cert-name and STARTTLS negotiation problems that make shared-hosting
SMTP fragile.
"""

from __future__ import annotations

import logging

import httpx

from .base import Message, Notifier, register

log = logging.getLogger(__name__)

ENDPOINT = "https://api.resend.com/emails"


@register
class ResendNotifier(Notifier):
    name = "resend"

    def available(self) -> tuple[bool, str]:
        s = self.settings
        if not s.resend_api_key:
            return False, "ITD_RESEND_API_KEY is not set"
        if not s.email_from:
            return False, "ITD_EMAIL_FROM is not set"
        if not s.email_to:
            return False, "ITD_EMAIL_TO is not set"
        return True, ""

    def send(self, message: Message) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"resend channel unavailable: {why}")

        s = self.settings
        payload = {
            "from": s.email_from,
            "to": list(s.email_to),
            "subject": message.subject,
            "text": message.text or "(no body)",
        }
        if message.html:
            payload["html"] = message.html

        try:
            response = httpx.post(
                ENDPOINT,
                json=payload,
                headers={
                    "Authorization": f"Bearer {s.resend_api_key}",
                    "Content-Type": "application/json",
                },
                timeout=s.request_timeout,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"resend request failed: {exc}") from exc

        if response.status_code not in (200, 201, 202):
            # The body carries the actual reason (unverified domain, bad key);
            # the status alone sends people hunting in the wrong place.
            raise RuntimeError(
                f"resend returned {response.status_code}: {(response.text or '')[:200]}"
            )
