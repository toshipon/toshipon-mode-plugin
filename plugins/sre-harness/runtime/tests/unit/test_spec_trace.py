"""Trace links from ids, paths, resource addresses and test names; reachability by basis."""

from sre_harness.spec import structure, trace
from tests.spec_helpers import decision

SHA = "a" * 40


def requirement(name, refs=()):
    return {
        "id": name,
        "external_id": name,
        "title": "ログの保持期間",
        "statement": "s",
        "decision_refs": list(refs),
        "basis": "explicit",
        "evidence": [],
        "ambiguity": [],
    }


def registry():
    decisions = [
        decision(2, status="superseded", superseded_by=["DEC-0005"]),
        decision(5, supersedes=["DEC-0002"]),
    ]
    return trace.Registry(decisions, [requirement("REQ-0001", ["DEC-0005"])])


def ends(links):
    return {(item["from"]["id"], item["to"]["id"], item["relation"]) for item in links}


def test_written_ids_resolve_to_registry_ids():
    reg = registry()
    assert reg.resolve("ADR-0002") == ("decision", "DEC-0002")
    assert reg.resolve("REQ-0001") == ("requirement", "REQ-0001")
    assert reg.resolve("REQ-0042") is None
    assert reg.node(("decision", "ADR-0005")) == ("decision", "DEC-0005")
    assert reg.node(("requirement", "REQ-0042")) == ("requirement", "REQ-0042")
    assert reg.entity(("requirement", "REQ-0001")) and not reg.entity(("design", "x.md"))
    assert reg.external("DEC-0002") == "ADR-0002" and reg.status("DEC-0002") == "superseded"
    assert reg.titles(11) == {}
    assert reg.titles(7)["REQ-0001"] == "ログの保持期間"


def test_explicit_links_from_ids_paths_addresses_and_test_names():
    reg = registry()
    doc = "# 設計\n\nREQ-0001 を満たす。`infra/logs.tf` の `aws_s3_bucket.logs` で実装し、ADR-0002 に従う。\n"
    units = structure.parse("docs/design.md", doc)
    links, dangling = trace.file_links(
        "docs/design.md",
        units,
        SHA,
        reg,
        addresses=["aws_s3_bucket.logs", "aws_s3_bucket.log"],
        paths=[("iac", "infra/logs.tf"), ("test", "tests/test_a.py")],
    )
    assert ends(links) == {
        ("docs/design.md", "REQ-0001", "refines"),
        ("docs/design.md", "DEC-0002", "depends_on"),
        ("aws_s3_bucket.logs", "docs/design.md", "implements"),
        ("infra/logs.tf", "docs/design.md", "implements"),
    }
    assert dangling == []
    assert all(item["basis"] == "explicit" and item["confidence"] == "high" for item in links)
    assert links[0]["from"]["sha"] == SHA and "sha" not in links[0]["to"]
    assert links[0]["evidence"]["ref"] == f"{SHA}:docs/design.md#L3"

    tests = structure.parse("tests/test_a.py", "def test_req_0001_x():\n    pass\n")
    links, _ = trace.file_links("tests/test_a.py", tests, SHA, reg)
    assert ends(links) == {("tests/test_a.py::test_req_0001_x", "REQ-0001", "verifies")}


def test_a_reference_to_an_unknown_id_is_dangling():
    units = structure.parse("docs/x.md", "# x\n\nREQ-0042 と ADR-0099。\n")
    links, dangling = trace.file_links("docs/x.md", units, SHA, registry())
    assert links == [] and sorted(item["ref"] for item in dangling) == ["ADR-0099", "REQ-0042"]


def test_supersedes_in_front_matter_is_conflicts_with_and_is_not_followed():
    doc = "---\nid: ADR-0005\nsupersedes: [ADR-0002]\n---\n\n# ADR-0005\n"
    reg = registry()
    links, _ = trace.file_links("docs/adr/5.md", structure.parse("docs/adr/5.md", doc), SHA, reg)
    assert ends(links) == {("DEC-0005", "DEC-0002", "conflicts_with")}
    reached = trace.reach({("decision", "DEC-0005")}, links)
    assert reached == {}


def test_registry_links_and_unique():
    reg = registry()
    links = trace.registry_links(reg)
    assert ends(links) == {("REQ-0001", "DEC-0005", "refines")}
    inferred = trace.link(
        ("requirement", "REQ-0001"), ("decision", "DEC-0005"), "refines", "inferred", "x"
    )
    other = trace.link(("design", "d.md"), ("requirement", "REQ-0001"), "refines", "inferred", "x")
    kept = trace.unique(links + links + [inferred, other])
    assert len(kept) == 2 and kept[1]["basis"] == "inferred" and kept[1]["confidence"] == "low"


def test_reach_marks_what_only_inferred_links_reach():
    explicit = trace.link(("test", "t"), ("requirement", "REQ-0001"), "verifies", "explicit", "a")
    upward = trace.link(
        ("requirement", "REQ-0001"), ("decision", "DEC-0005"), "refines", "inferred", "b"
    )
    rows = trace.numbered(
        [explicit, upward], "run-20260115T000000Z-0000abcd", "2026-01-15T00:00:00Z", 1
    )
    assert [row["id"] for row in rows] == ["TL-0001", "TL-0002"]
    reached = trace.reach({("test", "t")}, rows)
    assert reached[("requirement", "REQ-0001")] == ("explicit", ["TL-0001"])
    assert reached[("decision", "DEC-0005")] == ("inferred", ["TL-0001", "TL-0002"])
