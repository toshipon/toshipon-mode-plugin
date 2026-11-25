"""Invariant: an inferred trace link is never the only ground of a critical (or high) finding.

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
