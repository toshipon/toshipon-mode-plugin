"""Severity per kind, the cap on inferred evidence, ids and the invariant checker."""

import pytest

from sre_harness.spec import findings as fd


def test_inferred_evidence_alone_caps_the_severity():
    explicit = fd.draft(
        "semantic_conflict", ["REQ-0001"], [1, 2], [fd.evidence("a", "explicit")], "x"
    )
    inferred = fd.draft(
        "semantic_conflict", ["REQ-0001"], [1, 2], [fd.evidence("a", "inferred")], "x"
    )
    mixed = fd.draft(
        "superseded_dependency",
        ["DEC-0002"],
        [],
        [fd.evidence("a", "inferred"), fd.evidence("b", "explicit")],
        "x",
    )
    assert (explicit["severity"], explicit["basis"]) == ("critical", "explicit")
    assert (inferred["severity"], inferred["basis"]) == ("medium", "inferred")
    assert (mixed["severity"], mixed["basis"]) == ("high", "explicit")
    assert fd.draft("impact", [], [1], [fd.evidence("a", "inferred")], "x")["severity"] == "info"


@pytest.mark.parametrize("kind", fd.KINDS)
def test_no_kind_is_critical_or_high_on_inferred_evidence(kind):
    item = fd.draft(
        kind,
        [],
        [1, 2] if kind == "semantic_conflict" else [1],
        [fd.evidence("a", "inferred")],
        "x",
    )
    assert item["severity"] in ("medium", "low", "info")


def test_numbered_is_stable_and_check_holds():
    drafts = [
        fd.draft("impact", ["REQ-0001"], [3], [fd.evidence("b", "explicit")], "x"),
        fd.draft("semantic_conflict", ["REQ-0001"], [1, 2], [fd.evidence("a", "explicit")], "x"),
    ]
    found = fd.numbered(drafts)
    assert [(item["id"], item["kind"]) for item in found] == [
        ("FD-0001", "semantic_conflict"),
        ("FD-0002", "impact"),
    ]
    assert found == fd.numbered(list(reversed(drafts)))
    assert fd.check(found) == []
    assert found[0]["evidence"][0]["id"] == "E1"


def test_check_catches_findings_the_builder_never_makes():
    base = fd.numbered(
        [fd.draft("semantic_conflict", ["REQ-0001"], [1, 2], [fd.evidence("a", "explicit")], "x")]
    )[0]
    broken = [
        {**base, "basis": "inferred", "evidence": [{**base["evidence"][0], "basis": "inferred"}]},
        {
            **base,
            "basis": "explicit",
            "evidence": [{**base["evidence"][0], "basis": "inferred"}],
            "severity": "medium",
        },
        {**base, "id": "FD-0001", "basis": "inferred"},
        {**base, "id": "FD-0004", "prs": [1]},
        {**base, "id": "FD-0005", "kind": "missing_test", "prs": [1, 2]},
    ]
    problems = fd.check(broken)
    assert any("rests on inferred evidence only" in p for p in problems)
    assert any("without explicit evidence" in p for p in problems)
    assert any("used twice" in p for p in problems)
    assert any("although explicit evidence exists" in p for p in problems)
    assert any("needs two PRs" in p for p in problems)
    assert any("exactly one PR" in p for p in problems)
