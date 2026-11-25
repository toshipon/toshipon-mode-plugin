"""Invariant: an inferred trace link is never the only ground of a critical (or high) finding,
and neither is an inferred entity (a decision or requirement the backend proposed).

Held in three places, each checked here: the builder caps the severity, the checker refuses a
finding that breaks it, and the impact-report schema refuses it too.
"""

import pytest

from sre_harness import records
from sre_harness.spec import findings as fd


def _finding(severity, basis):
    return {
        "id": "FD-0001",
        "kind": "semantic_conflict",
        "severity": severity,
        "entities": ["REQ-0001"],
        "prs": [1, 2],
        "evidence": [{"id": "E1", "ref": "llm-calls.jsonl#LC-0001", "basis": basis}],
        "basis": basis,
        "explanation": "x",
    }


@pytest.mark.parametrize("severity", ["critical", "high"])
def test_the_schema_refuses_a_severe_finding_on_inferred_evidence(severity):
    assert records.violations("impact-report", _finding(severity, "inferred"), "finding") != []
    assert records.violations("impact-report", _finding(severity, "explicit"), "finding") == []


@pytest.mark.parametrize("severity", ["critical", "high"])
def test_the_checker_refuses_it(severity):
    assert fd.check([_finding(severity, "inferred")]) != []


@pytest.mark.parametrize("kind", fd.KINDS)
def test_the_builder_never_makes_it(kind):
    prs = [1, 2] if kind == "semantic_conflict" else [1]
    item = fd.draft(kind, [], prs, [fd.evidence("x", "inferred")], "x")
    assert fd.SEVERITIES.index(item["severity"]) >= fd.SEVERITIES.index(fd.INFERRED_CAP)
    assert fd.check(fd.numbered([item])) == []


def _about_inferred(severity, basis):
    finding = _finding(severity, basis)
    return {**finding, "entities": ["RQI-0001"], "inferred_entities": ["RQI-0001"]}


@pytest.mark.parametrize("severity", ["critical", "high"])
def test_a_finding_about_an_inferred_entity_is_never_severe(severity):
    for basis in ("explicit", "inferred"):
        assert records.violations("impact-report", _about_inferred(severity, basis), "finding")
        assert fd.check([_about_inferred(severity, basis)]) != []
    assert records.violations("impact-report", _about_inferred("medium", "explicit"), "finding")
    assert fd.check([_about_inferred("medium", "explicit")]) != []
    assert fd.check([_about_inferred("medium", "inferred")]) == []


@pytest.mark.parametrize("kind", fd.KINDS)
def test_the_builder_caps_a_finding_about_an_inferred_entity(kind):
    prs = [1, 2] if kind == "semantic_conflict" else [1]
    item = fd.draft(kind, ["RQI-0001", "REQ-0001"], prs, [fd.evidence("x", "explicit")], "x")
    (marked,) = fd.mark_inferred([item], {"RQI-0001", "DCI-0001"})
    assert marked["basis"] == "inferred" and marked["inferred_entities"] == ["RQI-0001"]
    assert fd.SEVERITIES.index(marked["severity"]) >= fd.SEVERITIES.index(fd.INFERRED_CAP)
    assert fd.check(fd.numbered([marked])) == []
    (plain,) = fd.mark_inferred(
        [fd.draft(kind, ["REQ-0001"], prs, [fd.evidence("x", "explicit")], "x")], {"RQI-0001"}
    )
    assert plain["basis"] == "explicit" and "inferred_entities" not in plain
