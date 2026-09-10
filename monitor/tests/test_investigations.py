from __future__ import annotations

import pytest

from intothedarkness.investigations import CaseManager, slugify
from intothedarkness.models import Finding, FindingKind, Item, Severity


@pytest.fixture
def cases(repo, settings) -> CaseManager:
    return CaseManager(repo, settings)


def test_slugify():
    assert slugify("Fake Support Portals!") == "fake-support-portals"
    assert slugify("...") == "case"


def test_open_case_and_reject_duplicates(cases):
    case = cases.open_case("Fake support portals", tags=["phishing"])
    assert case.slug == "fake-support-portals"
    assert case.status == "open"
    with pytest.raises(ValueError, match="already exists"):
        cases.open_case("Fake support portals")


def test_unknown_case_raises(cases):
    with pytest.raises(KeyError, match="no case named"):
        cases.add_note("nope", "hi")


def test_notes_and_links_land_on_the_timeline(cases):
    cases.open_case("Case one")
    cases.add_note("case-one", "First observation.")
    cases.add_link("case-one", "https://e.com/evidence", note="registrar record")

    timeline = cases.timeline("case-one")
    assert [e["type"] for e in timeline] == ["note", "link"]
    assert "evidence" in timeline[1]["label"]


def test_attached_files_are_copied_and_hashed(cases, tmp_path):
    source = tmp_path / "screenshot.png"
    source.write_bytes(b"not really a png")

    cases.open_case("Case two")
    stored = cases.add_file("case-two", source, note="portal screenshot")

    assert stored.exists()
    assert stored.read_bytes() == source.read_bytes()

    event = cases.timeline("case-two")[0]
    assert event["type"] == "file"
    assert len(event["sha256"]) == 64
    # Editing the original must not change the stored copy or its recorded hash.
    source.write_bytes(b"tampered")
    assert stored.read_bytes() == b"not really a png"


def test_attaching_a_missing_file_errors(cases, tmp_path):
    cases.open_case("Case three")
    with pytest.raises(FileNotFoundError):
        cases.add_file("case-three", tmp_path / "absent.bin")


def test_findings_can_be_attached_and_appear_in_the_timeline(cases, repo):
    ids = repo.save_findings([
        Finding(
            kind=FindingKind.NEW,
            target="t",
            severity=Severity.HIGH,
            item=Item(key="k", target="t", title="Suspicious portal", url="https://e.com/p"),
        )
    ])
    cases.open_case("Case four")
    assert cases.attach_findings("case-four", ids) == 1

    event = cases.timeline("case-four")[0]
    assert event["type"] == "finding"
    assert "high" in event["label"]


def test_export_markdown_contains_evidence_and_hashes(cases, tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("evidence body")

    cases.open_case("Case five", tags=["phishing"])
    cases.add_note("case-five", "A note worth reading.")
    cases.add_file("case-five", source)
    cases.close_case("case-five", summary="Concluded: it was a duck.")

    path = cases.export_markdown("case-five")
    text = path.read_text()

    assert "# Case five" in text
    assert "closed" in text
    assert "A note worth reading." in text
    assert "sha256:" in text
    assert "Concluded: it was a duck." in text
    assert "phishing" in text


def test_export_json_round_trips(cases):
    import json

    cases.open_case("Case six")
    cases.add_note("case-six", "note")
    payload = json.loads(cases.export_json("case-six").read_text())

    assert payload["slug"] == "case-six"
    assert len(payload["timeline"]) == 1


def test_timeline_is_chronological(cases, tmp_path):
    cases.open_case("Case seven")
    for i in range(3):
        cases.add_note("case-seven", f"note {i}")
    timeline = cases.timeline("case-seven")
    assert [e["at"] for e in timeline] == sorted(e["at"] for e in timeline)
