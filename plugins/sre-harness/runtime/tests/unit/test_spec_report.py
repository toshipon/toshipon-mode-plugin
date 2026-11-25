"""Escaping external text in the Markdown report, and the evaluator-output shape."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sre_harness.spec import findings as fd
from sre_harness.spec import report

V2_SCHEMA = (
    Path(__file__).resolve().parents[5]
    / "plugins/toshipon-mode/skills/autonomous-improvement/schemas/evaluation.schema.json"
)
HOSTILE = "[x](http://example.invalid/a) ![](http://example.invalid/i.png) | `code` <b>b</b> &"


def test_a_cell_shows_links_images_html_and_pipes_as_text():
    text = report.cell(HOSTILE + "\nnext line")
    assert "](" not in text.replace("\\]\\(", "")
    assert "\\[x\\]\\(" in text and "\\!\\[\\]" in text
    assert "<b>" not in text and "&lt;b&gt;" in text and "&amp;" in text
    assert "\\|" in text and "| " not in text.replace("\\| ", "")
    assert "\\`code\\`" in text
    assert "://" not in text and "\n" not in text


@pytest.mark.parametrize(
    "value",
    [
        "see www.example.invalid/x now",
        "WWW.example.invalid",
        "mail admin@example.invalid",
        "https://example.invalid/a",
        "mailto:admin@example.invalid",
    ],
)
def test_a_cell_leaves_no_gfm_autolink(value):
    text = report.cell(value)
    assert "www." not in text.lower() and "@" not in text and "://" not in text


def test_a_block_cannot_be_closed_by_its_content():
    text = "a\n```\n````\nb"
    out = report.block(text)
    fence = out.splitlines()[0][: -len("text")]
    assert fence == "`" * 5
    assert out.splitlines()[-1] == fence
    assert text in out
    assert report.block("plain").startswith("```text\n")


def _report(prs, findings=()):
    return {
        "run_id": "run-20260115T000000Z-0000abcd",
        "repo": "example-org/demo",
        "status": "complete",
        "criteria_hash": "0" * 64,
        "default_branch": {"name": "main", "sha": "a" * 40, "moved_since_analysis": False},
        "prs": prs,
        "findings": list(findings),
        "limitations": ["note with [link](http://example.invalid)"],
    }


def _pr(number, stale=False, missing=None):
    value = {
        "number": number,
        "head_sha": "a" * 40,
        "merge_base": None,
        "analyzed_head_sha": None,
        "stale": stale,
        "title": HOSTILE,
    }
    if missing:
        value["missing"] = missing
    return value


def test_evaluator_output_fits_the_v2_schema_and_says_when_nothing_was_analyzed():
    schema = json.loads(V2_SCHEMA.read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/output"})
    found = fd.numbered(
        [fd.draft("stale_sha", [], [1], [fd.evidence("pulls#1", "explicit", "x")], "moved")]
    )
    output = report.evaluator_output(_report([_pr(1, stale=True), _pr(2, missing="gone")], found))
    assert list(validator.iter_errors(output)) == []
    assert output["metrics"]["prs_stale"] == 1 and output["metrics"]["prs_unanalyzed"] == 1
    assert output["metrics"]["findings_stale_sha"] == 1
    assert output["unevaluable"].startswith("no open PR")
    assert output["evidence"] == [
        {"id": "FD-0001", "ref": "impact-report.json#FD-0001", "note": "stale_sha high explicit"}
    ]
    assert "unevaluable" not in report.evaluator_output(_report([]))


def test_the_markdown_keeps_external_text_out_of_markup():
    item = fd.numbered(
        [
            fd.draft(
                "impact",
                ["REQ-0001"],
                [1],
                [fd.evidence("a", "explicit", HOSTILE), fd.evidence("b", "inferred", "q")],
                "explanation " + HOSTILE,
            )
        ]
    )
    links = {
        "links": [
            {
                "id": "TL-0001",
                "from": {"type": "design", "id": "docs/[x](y).md"},
                "to": {"type": "requirement", "id": "REQ-0001"},
                "relation": "refines",
                "basis": "inferred",
            }
        ]
    }
    text = report.markdown(_report([_pr(1)], item), links)
    lines = text.splitlines()
    inside, outside = False, []
    for line in lines:
        if line.startswith("```"):
            inside = not inside
            continue
        if not inside:
            outside.append(line)
    joined = "\n".join(outside)
    assert HOSTILE not in joined and "](http" not in joined and "<b>" not in joined
    assert HOSTILE in text  # in a code block, as written
    assert "| FD-0001 | impact | info | explicit | REQ-0001 | #1 | E1 | E2 |" in text
    assert "| - | TL-0001 refines |" in text


def test_moved_since_analysis_is_unchecked_not_false():
    value = {**_report([_pr(1)]), "generated_at": "2026-01-15T00:00:00Z", "schema_version": 1}
    value["default_branch"] = {**value["default_branch"], "moved_since_analysis": None}
    from sre_harness import records

    assert records.violations("impact-report", value) == []
