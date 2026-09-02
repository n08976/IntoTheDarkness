"""Load targets and rules from YAML."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from .alerting.rules import Rule, RuleSet
from .enrich import SectorClassifier
from .models import Target


class ConfigError(ValueError):
    """A targets or rules file is malformed."""


def _read_yaml(path: Path) -> object:
    if not path.exists():
        raise ConfigError(f"{path} does not exist (run `itd init` to create a starter)")
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc


def _entries(data: object, key: str, path: Path) -> list[dict]:
    """Accept either a bare list or a mapping with a top-level key."""
    if isinstance(data, list):
        entries = data
    elif isinstance(data, dict):
        entries = data.get(key, [])
    else:
        raise ConfigError(f"{path}: expected a list or a mapping with '{key}'")
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: '{key}' must be a list")
    return entries


def load_targets(path: Path) -> list[Target]:
    entries = _entries(_read_yaml(path), "targets", path)
    targets: list[Target] = []
    seen: set[str] = set()

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: target #{index + 1} is not a mapping")
        try:
            target = Target.model_validate(entry)
        except ValidationError as exc:
            name = entry.get("name", f"#{index + 1}")
            raise ConfigError(f"{path}: target {name!r} is invalid:\n{exc}") from exc
        if target.name in seen:
            raise ConfigError(f"{path}: duplicate target name {target.name!r}")
        seen.add(target.name)
        targets.append(target)

    return targets


def load_rules(path: Path) -> RuleSet:
    if not path.exists():
        return RuleSet()  # rules are optional; no file means "alert on everything"
    entries = _entries(_read_yaml(path), "rules", path)
    rules: list[Rule] = []

    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: rule #{index + 1} is not a mapping")
        try:
            rules.append(Rule.model_validate(entry))
        except ValidationError as exc:
            name = entry.get("name", f"#{index + 1}")
            raise ConfigError(f"{path}: rule {name!r} is invalid:\n{exc}") from exc

    return RuleSet(rules=rules)


def load_sectors(path: Path) -> SectorClassifier:
    """Sector keywords are optional; without a file the built-ins are used."""
    return SectorClassifier.load(path)
