"""Print to the terminal. The default channel, and the one used by --dry-run."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ..models import Severity
from .base import Message, Notifier, register

_console = Console()

_STYLE = {
    Severity.INFO: "dim",
    Severity.LOW: "blue",
    Severity.MEDIUM: "yellow",
    Severity.HIGH: "red",
    Severity.CRITICAL: "bold white on red",
}


@register
class ConsoleNotifier(Notifier):
    name = "console"

    def send(self, message: Message) -> None:
        # Body and subject are built from scraped content, so they are rendered
        # as Text rather than markup: a page containing "[info]" or "[/]" must
        # not be able to style — or silently blank — our own output.
        _console.print(
            Panel(
                Text(message.text or "(no body)"),
                title=Text(message.subject),
                border_style=_STYLE.get(message.max_severity, "dim"),
                expand=False,
            )
        )
