"""Notification channels. Importing this package registers the built-ins."""

from .base import REGISTRY, Message, Notifier, available, get_notifier, register
from .console import ConsoleNotifier
from .email import EmailNotifier
from .preview import PreviewNotifier
from .render import (
    render_digest_html,
    render_digest_text,
    render_html,
    render_subject,
    render_text,
)
from .resend import ResendNotifier
from .webhook import WebhookNotifier

__all__ = [
    "REGISTRY",
    "ConsoleNotifier",
    "EmailNotifier",
    "Message",
    "PreviewNotifier",
    "ResendNotifier",
    "Notifier",
    "WebhookNotifier",
    "available",
    "get_notifier",
    "register",
    "render_digest_html",
    "render_digest_text",
    "render_html",
    "render_subject",
    "render_text",
]
