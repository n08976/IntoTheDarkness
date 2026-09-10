"""POST findings as JSON to an arbitrary endpoint (Slack, Discord, your own)."""

from __future__ import annotations

import httpx

from .base import Message, Notifier, register


@register
class WebhookNotifier(Notifier):
    name = "webhook"

    def available(self) -> tuple[bool, str]:
        if not self.settings.webhook_url:
            return False, "ITD_WEBHOOK_URL is not set"
        return True, ""

    def send(self, message: Message) -> None:
        ok, why = self.available()
        if not ok:
            raise RuntimeError(f"webhook channel unavailable: {why}")

        payload = {
            "subject": message.subject,
            # Slack and Discord both read a top-level "text" field.
            "text": f"*{message.subject}*\n{message.text}",
            "severity": message.max_severity.value,
            "findings": [f.model_dump(mode="json") for f in message.findings],
        }
        resp = httpx.post(
            self.settings.webhook_url, json=payload, timeout=self.settings.request_timeout
        )
        resp.raise_for_status()
