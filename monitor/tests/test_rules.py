from __future__ import annotations

from intothedarkness.alerting import Rule, RuleSet
from intothedarkness.models import Finding, FindingKind, Item, Severity


def finding(target="t", kind=FindingKind.NEW, title="Nothing special", **kw) -> Finding:
    return Finding(
        kind=kind,
        target=target,
        item=Item(key="k", target=target, title=title, url="https://e.com/1"),
        **kw,
    )


def test_ignore_action_drops_the_finding():
    rules = RuleSet(rules=[Rule(name="drop", action="ignore", match="newsletter")])
    kept = rules.apply([finding(title="newsletter signup"), finding(title="real news")])
    assert [f.item.title for f in kept] == ["real news"]


def test_severity_is_raised_not_lowered():
    rules = RuleSet(rules=[Rule(name="up", match="breach", severity=Severity.CRITICAL)])
    raised = rules.apply([finding(title="data breach")])[0]
    assert raised.severity is Severity.CRITICAL

    rules = RuleSet(rules=[Rule(name="down", match="breach", severity=Severity.LOW)])
    unchanged = rules.apply([finding(title="data breach", severity=Severity.HIGH)])[0]
    assert unchanged.severity is Severity.HIGH


def test_channels_accumulate_without_duplicates():
    rules = RuleSet(rules=[
        Rule(name="a", channels=["email"]),
        Rule(name="b", channels=["email", "webhook"]),
    ])
    result = rules.apply([finding()])[0]
    assert result.channels == ["email", "webhook"]


def test_stop_prevents_later_rules():
    rules = RuleSet(rules=[
        Rule(name="first", match="breach", severity=Severity.HIGH, stop=True),
        Rule(name="second", match="breach", channels=["email"]),
    ])
    result = rules.apply([finding(title="breach")])[0]
    assert result.severity is Severity.HIGH
    assert result.channels == []
    assert result.rule == "first"


def test_target_globs():
    rules = RuleSet(rules=[Rule(name="prod", targets=["prod-*"], channels=["email"])])
    kept = rules.apply([finding(target="prod-web"), finding(target="dev-web")])
    assert kept[0].channels == ["email"]
    assert kept[1].channels == []


def test_kind_and_tag_conditions():
    rules = RuleSet(rules=[Rule(name="errs", kinds=[FindingKind.ERROR], channels=["console"])])
    kept = rules.apply([finding(kind=FindingKind.ERROR), finding(kind=FindingKind.NEW)])
    assert kept[0].channels == ["console"] and kept[1].channels == []

    rules = RuleSet(rules=[Rule(name="tagged", tags=["status"], channels=["email"])])
    kept = rules.apply([finding(target="a"), finding(target="b")], {"a": ["status"], "b": ["news"]})
    assert kept[0].channels == ["email"] and kept[1].channels == []


def test_not_match_excludes():
    rules = RuleSet(rules=[Rule(name="r", match="alert", not_match="drill", channels=["email"])])
    kept = rules.apply([finding(title="alert: real"), finding(title="alert: drill only")])
    assert kept[0].channels == ["email"] and kept[1].channels == []


def test_disabled_rules_do_nothing():
    rules = RuleSet(rules=[Rule(name="off", enabled=False, action="ignore", match=".")])
    assert len(rules.apply([finding()])) == 1


def test_empty_ruleset_passes_everything_through():
    findings = [finding(), finding(title="other")]
    assert RuleSet().apply(findings) == findings


# ------------------------------------------------------- sector filtering


def sector_finding(name, sector) -> Finding:
    return Finding(
        kind=FindingKind.NEW,
        target="dls-example",
        item=Item(key=name, target="dls-example", title=name, fields={"sector": sector}),
    )


def test_sectors_condition_selects_one_industry():
    rules = RuleSet(rules=[Rule(name="health", sectors=["healthcare"], channels=["email"])])
    kept = rules.apply(
        [sector_finding("Clinic", "healthcare"), sector_finding("Steel", "manufacturing")]
    )
    assert kept[0].channels == ["email"]
    assert kept[1].channels == []


def test_sectors_condition_is_case_insensitive():
    rules = RuleSet(rules=[Rule(name="h", sectors=["HealthCare"], channels=["email"])])
    assert rules.apply([sector_finding("Clinic", "healthcare")])[0].channels == ["email"]


def test_findings_without_a_sector_never_match_a_sector_rule():
    rules = RuleSet(rules=[Rule(name="h", sectors=["healthcare"], channels=["email"])])
    bare = Finding(kind=FindingKind.NEW, target="t", message="no item")
    assert rules.apply([bare])[0].channels == []


def test_only_this_sector_recipe_needs_stop():
    """The documented recipe keeps just one sector and drops the rest."""
    findings = [
        sector_finding("Clinic", "healthcare"),
        sector_finding("Steel", "manufacturing"),
    ]
    recipe = RuleSet(rules=[
        Rule(name="keep-health", sectors=["healthcare"], channels=["email"], stop=True),
        Rule(name="drop-rest", action="ignore"),
    ])
    kept = recipe.apply(list(findings))
    assert [f.item.title for f in kept] == ["Clinic"]

    # Without `stop`, the catch-all swallows the kept findings as well.
    trap = RuleSet(rules=[
        Rule(name="keep-health", sectors=["healthcare"], channels=["email"]),
        Rule(name="drop-rest", action="ignore"),
    ])
    assert trap.apply(list(findings)) == []


def test_ordering_warning_flags_the_trap_and_stays_quiet_when_correct():
    from intothedarkness.alerting import ordering_warnings

    trap = [
        Rule(name="keep-health", sectors=["healthcare"], channels=["email"]),
        Rule(name="drop-rest", action="ignore"),
    ]
    warnings = ordering_warnings(trap)
    assert len(warnings) == 1
    assert "keep-health" in warnings[0] and "stop: true" in warnings[0]

    correct = [
        Rule(name="keep-health", sectors=["healthcare"], channels=["email"], stop=True),
        Rule(name="drop-rest", action="ignore"),
    ]
    assert ordering_warnings(correct) == []


def test_a_narrowed_ignore_rule_is_not_a_catch_all():
    from intothedarkness.alerting import ordering_warnings

    narrowed = [
        Rule(name="keep", sectors=["healthcare"], channels=["email"]),
        Rule(name="drop-noise", action="ignore", match="newsletter"),
    ]
    assert ordering_warnings(narrowed) == []
