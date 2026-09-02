from __future__ import annotations

import pytest

from intothedarkness.loader import ConfigError, load_rules, load_targets
from intothedarkness.models import FindingKind, Severity


def write(path, text):
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_targets_from_a_mapping(tmp_path):
    path = write(tmp_path / "t.yaml", """
targets:
  - name: alpha
    url: https://example.com/
    scraper: css
    selectors:
      item: "li"
      title: "a"
    watch: [new, changed]
    severity: high
    tags: [x]
""")
    targets = load_targets(path)
    assert len(targets) == 1
    assert targets[0].selectors.item == "li"
    assert targets[0].watch == [FindingKind.NEW, FindingKind.CHANGED]
    assert targets[0].severity is Severity.HIGH


def test_loads_targets_from_a_bare_list(tmp_path):
    path = write(tmp_path / "t.yaml", "- name: alpha\n  url: https://example.com/\n")
    assert load_targets(path)[0].name == "alpha"


def test_missing_file_is_a_helpful_error(tmp_path):
    with pytest.raises(ConfigError, match="does not exist"):
        load_targets(tmp_path / "nope.yaml")


def test_bad_yaml_is_reported_as_config_error(tmp_path):
    path = write(tmp_path / "t.yaml", "targets: [\n  - name: unclosed")
    with pytest.raises(ConfigError, match="not valid YAML"):
        load_targets(path)


def test_unknown_field_is_rejected_by_name(tmp_path):
    path = write(
        tmp_path / "t.yaml",
        "targets:\n  - name: a\n    url: https://e.com/\n    typo: 1\n",
    )
    with pytest.raises(ConfigError, match="'a' is invalid"):
        load_targets(path)


def test_duplicate_names_are_rejected(tmp_path):
    path = write(tmp_path / "t.yaml", """
targets:
  - name: same
    url: https://e.com/1
  - name: same
    url: https://e.com/2
""")
    with pytest.raises(ConfigError, match="duplicate target name"):
        load_targets(path)


def test_rules_file_is_optional(tmp_path):
    assert load_rules(tmp_path / "absent.yaml").rules == []


def test_loads_rules(tmp_path):
    path = write(tmp_path / "r.yaml", """
rules:
  - name: escalate
    match: breach
    severity: critical
    channels: [email]
    stop: true
""")
    rules = load_rules(path).rules
    assert rules[0].severity is Severity.CRITICAL and rules[0].stop
