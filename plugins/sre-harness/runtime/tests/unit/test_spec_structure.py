"""The fixed rules that read Markdown, IaC text and tests into units, references and facts."""

import pytest

from sre_harness.spec import structure

DOC = """\
---
id: ADR-0010
title: x
status: approved
supersedes: [ADR-0004]
owner_team: sre
---

# ADR-0010: x

- retention_days: 30

See REQ-0001.
"""

DESIGN = """\
# 設計

前置き。

## 保持（REQ-0001）

- retention_days: 400
- Given: not a fact

### 内訳

- archive_days: 10

## REQ-0004: 定義

- timeout_seconds: 10
Decisions: ADR-0002

## 関係なし

- loose_key: 1
"""

TF = """\
# Implements REQ-0001.
resource "aws_s3_bucket" "logs" {
  bucket         = "logs" # not an id
  retention_days = 400 # REQ-0001
  nested {
    days = 1 # REQ-0002
  }
}

data "aws_caller_identity" "me" {}

module "network" {
  source = "./network" // ADR-0003
}
"""

TESTS = """\
import pytest


def test_req_0001_retention():
    assert True


async def test_plain():
    assert True


it("verifies REQ-0004 and adr_0002", () => {})
"""


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("infra/logs.tf", "iac"),
        ("tests/test_x.py", "test"),
        ("pkg/thing_test.go", "test"),
        ("web/a.test.ts", "test"),
        ("test_top.py", "test"),
        ("docs/a.md", "markdown"),
        ("README.markdown", "markdown"),
        ("src/app.py", None),
        ("tests/data.json", None),
    ],
)
def test_file_kind_comes_from_the_path(path, kind):
    assert structure.file_kind(path) == kind


def test_markdown_front_matter_id_defines_the_document():
    units = structure.parse("docs/adr/0010.md", DOC)
    assert {unit["node"] for unit in units} == {("decision", "ADR-0010")}
    front = units[0]
    assert [(r["id"], r["hint"]) for r in front["refs"]] == [("ADR-0004", "supersedes")]
    assert ("ADR-0010", "owner_team", "sre", 6) in front["facts"]
    found, _ = structure.facts(units)
    assert found[("ADR-0010", "retention_days")][0] == "30"
    assert any(r["id"] == "REQ-0001" for unit in units for r in unit["refs"])


def test_markdown_sections_scope_facts_and_nodes_by_heading():
    units = structure.parse("docs/design.md", DESIGN)
    by_heading = {unit["start"]: unit for unit in units}
    facts, conflicts = structure.facts(units)
    assert conflicts == []
    assert facts[("REQ-0001", "retention_days")] == ("400", 7)
    assert facts[("REQ-0001", "archive_days")] == ("10", 12)  # a subsection inherits the id
    assert facts[("REQ-0004", "timeout_seconds")] == ("10", 16)
    assert ("REQ-0001", "given") not in facts and ("REQ-0001", "Given") not in facts
    assert not any(key == "loose_key" for _, key in facts)  # no id in the heading
    nodes = [unit["node"] for unit in units]
    assert ("design", "docs/design.md") in nodes and ("requirement", "REQ-0004") in nodes
    defining = [u for u in units if u["node"] == ("requirement", "REQ-0004")][0]
    assert [r["id"] for r in defining["refs"]] == ["ADR-0002"]
    assert by_heading[1]["node"] == ("design", "docs/design.md")


def test_iac_blocks_have_addresses_comment_refs_and_tagged_attributes():
    units = structure.parse("infra/main.tf", TF)
    assert [unit["node"][1] for unit in units] == [
        "aws_s3_bucket.logs",
        "data.aws_caller_identity.me",
        "module.network",
    ]
    logs = units[0]
    assert logs["start"] == 1 and logs["end"] == 8
    assert {ref["id"] for ref in logs["refs"]} == {"REQ-0001", "REQ-0002"}
    # Only a top-level attribute with an id in its comment is a fact.
    assert [(e, k, v) for e, k, v, _ in logs["facts"]] == [("REQ-0001", "retention_days", "400")]
    assert [ref["id"] for ref in units[2]["refs"]] == ["ADR-0003"]


def test_test_names_carry_ids():
    units = structure.parse("tests/test_a.py", TESTS)
    names = [unit["node"][1] for unit in units]
    assert names == [
        "tests/test_a.py::test_req_0001_retention",
        "tests/test_a.py::test_plain",
        "tests/test_a.py::verifies REQ-0004 and adr_0002",
    ]
    assert [r["id"] for r in units[0]["refs"]] == ["REQ-0001"]
    assert units[1]["refs"] == []
    assert sorted(r["id"] for r in units[2]["refs"]) == ["ADR-0002", "REQ-0004"]


def test_an_unread_kind_has_no_units():
    assert structure.parse("src/app.py", "REQ-0001 = 1\n") == []


def test_a_second_value_in_one_file_is_reported():
    text = "## REQ-0001\n\n- days: 1\n- days: 2\n"
    found, conflicts = structure.facts(structure.parse("a.md", text))
    assert found[("REQ-0001", "days")] == ("1", 3)
    assert conflicts == [("REQ-0001", "days", "2", 4)]


def test_touched_units_overlap_the_ranges():
    units = structure.parse("docs/design.md", DESIGN)
    touched = structure.touched(units, [(7, 7)])
    assert [unit["start"] for unit in touched] == [5]
    assert structure.touched(units, []) == []
