"""Importing leak-site addresses from a ransomwatch-format groups.json."""

from __future__ import annotations

import json

import pytest

from intothedarkness.importers import (
    build_targets,
    load_groups,
    parse_groups,
    seed_keys_from_posts,
    to_yaml,
    validate_targets,
)
from intothedarkness.models import Target

V3_A = "a" * 56 + ".onion"
V3_B = "b" * 56 + ".onion"
V2 = "c" * 16 + ".onion"


def host(fqdn, available=True, enabled=True, version=3):
    return {
        "fqdn": fqdn,
        "title": None,
        "version": version,
        "available": available,
        "enabled": enabled,
        "updated": "2026-09-01 00:00:00.000000",
    }


def group(name, hosts, **kw):
    return {
        "name": name,
        "captcha": kw.get("captcha", False),
        "javascript_render": kw.get("javascript", False),
        "parser": kw.get("parser", False),
        "meta": kw.get("meta", ""),
        "locations": hosts,
        "profile": [],
    }


GROUPS = [
    group("livegroup", [host(V3_A), host(V3_B)], parser=True),
    group("downgroup", [host(V3_A, available=False)]),
    group("disabledgroup", [host(V3_A, enabled=False)]),
    group("v2group", [host(V2, version=2)]),
    group("captchagroup", [host(V3_A)], captcha=True),
    group("jsgroup", [host(V3_A)], javascript=True),
]


# ----------------------------------------------------------------------- parsing


def test_parses_group_and_host_fields():
    groups = parse_groups(GROUPS)
    assert len(groups) == 6
    live = groups[0]
    assert live.name == "livegroup" and live.has_parser
    assert len(live.usable_hosts) == 2


def test_rejects_a_non_list_document():
    with pytest.raises(ValueError, match="must be a list"):
        parse_groups({"groups": []})


def test_entries_without_a_name_are_ignored():
    assert parse_groups([{"locations": []}, group("ok", [host(V3_A)])])[0].name == "ok"


def test_loads_from_disk(tmp_path):
    path = tmp_path / "groups.json"
    path.write_text(json.dumps(GROUPS))
    assert len(load_groups(path)) == 6


# ------------------------------------------------------------------- selection


def test_only_reachable_v3_groups_become_targets():
    report = build_targets(parse_groups(GROUPS))
    assert [t["name"] for t in report.targets] == ["dls-livegroup"]


def test_skip_reasons_are_explicit():
    report = build_targets(parse_groups(GROUPS))
    assert report.skipped == {
        "downgroup": "no reachable v3 mirror",
        "disabledgroup": "no reachable v3 mirror",
        "v2group": "no reachable v3 mirror",
        "captchagroup": "captcha-gated",
        "jsgroup": "needs JavaScript rendering",
    }


def test_unavailable_mirrors_can_be_included_deliberately():
    report = build_targets(parse_groups(GROUPS), include_unavailable=True)
    names = {t["name"] for t in report.targets}
    assert "dls-downgroup" in names
    assert "dls-v2group" not in names       # v2 is unroutable, not merely down
    assert "dls-disabledgroup" not in names # disabled upstream stays disabled


def test_extra_mirrors_are_recorded_not_duplicated():
    """Mirrors serve the same content; separate targets would double-report."""
    report = build_targets(parse_groups(GROUPS))
    assert len(report.targets) == 1
    assert V3_B in report.targets[0]["notes"]
    assert report.targets[0]["url"] == f"http://{V3_A}/"


def test_group_filter():
    report = build_targets(parse_groups(GROUPS), only={"captchagroup"})
    assert report.targets == []          # still skipped for its captcha
    assert "captchagroup" in report.skipped


# --------------------------------------------------------------------- output


def test_imported_targets_are_disabled_and_safe_by_default():
    target = build_targets(parse_groups(GROUPS)).targets[0]
    assert target["enabled"] is False
    assert target["network"] == "tor"
    assert target["scraper"] == "page"       # no selectors needed to start
    assert target["content_mode"] == "hash"  # no bodies on disk by default
    assert target["report_baseline"] is True


def test_dls_mode_emits_selector_placeholders():
    target = build_targets(parse_groups(GROUPS), scraper="dls").targets[0]
    assert target["scraper"] == "dls"
    assert target["selectors"]["item"] == "TODO"
    assert target["watch"] == ["new", "removed"]


def test_generated_targets_validate_as_real_targets():
    for scraper in ("page", "dls"):
        report = build_targets(parse_groups(GROUPS), scraper=scraper)
        assert validate_targets(report.targets) == []


def test_yaml_round_trips_into_targets():
    import yaml

    report = build_targets(parse_groups(GROUPS))
    parsed = yaml.safe_load(to_yaml(report.targets))
    assert Target.model_validate(parsed["targets"][0]).name == "dls-livegroup"


def test_tags_carry_the_group_name():
    target = build_targets(parse_groups(GROUPS)).targets[0]
    assert set(target["tags"]) == {"dls", "ransomware", "livegroup"}


# ------------------------------------------------------------------ post seeding


POSTS = [
    {"post_title": "Acme Steel Ltd", "group_name": "g1", "discovered": "2026-01-01"},
    {"post_title": "ACME STEEL LIMITED", "group_name": "g1", "discovered": "2026-02-01"},
    {"post_title": "St Mary Hospital", "group_name": "g2", "discovered": "2026-03-01"},
    {"post_title": "   ", "group_name": "g1", "discovered": "2026-04-01"},
]


def test_seeding_deduplicates_by_victim_identity(tmp_path):
    path = tmp_path / "posts.json"
    path.write_text(json.dumps(POSTS))
    pairs = seed_keys_from_posts(path)
    # The two Acme spellings collapse; the blank title is dropped.
    assert len(pairs) == 2
    assert {t for _, t in pairs} == {"Acme Steel Ltd", "St Mary Hospital"}


def test_seeding_can_filter_to_one_group(tmp_path):
    path = tmp_path / "posts.json"
    path.write_text(json.dumps(POSTS))
    assert [t for _, t in seed_keys_from_posts(path, group="g2")] == ["St Mary Hospital"]


def test_seeded_keys_match_what_the_dls_scraper_produces(tmp_path):
    """Seeding is pointless unless the keys line up with scraped items."""
    from intothedarkness.models import stable_hash
    from intothedarkness.scrapers.dls import identity_key, normalize_name

    path = tmp_path / "posts.json"
    path.write_text(json.dumps(POSTS))
    seeded = {stable_hash("dls", key) for key, _ in seed_keys_from_posts(path)}

    scraped = stable_hash("dls", identity_key(normalize_name("ACME Steel Ltd [READ MORE]")))
    assert scraped in seeded
