"""Notification channels."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from ..config import Settings
from ..models import Finding, Severity


@dataclass(slots=True)
class Message:
    subject: str
    text: str
    html: str = ""
    findings: Sequence[Finding] = ()

    @property
    def max_severity(self) -> Severity:
        if not self.findings:
            return Severity.INFO
        return max((f.severity for f in self.findings), key=lambda s: s.rank)


class Notifier(ABC):
    name: str = "base"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @abstractmethod
    def send(self, message: Message) -> None:
        """Deliver the message, or raise on failure."""

    def available(self) -> tuple[bool, str]:
        """Whether this channel is configured; second element explains why not."""
        return True, ""


REGISTRY: dict[str, type[Notifier]] = {}


def register(cls: type[Notifier]) -> type[Notifier]:
    REGISTRY[cls.name] = cls
    return cls


def get_notifier(name: str, settings: Settings) -> Notifier:
    try:
        cls = REGISTRY[name]
    except KeyError:
        known = ", ".join(sorted(REGISTRY)) or "(none)"
        raise KeyError(f"unknown channel {name!r}; registered: {known}") from None
    return cls(settings)


def available() -> list[str]:
    return sorted(REGISTRY)
