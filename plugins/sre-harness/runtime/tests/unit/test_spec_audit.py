"""`spec audit` rules: the axis status table, the evidence validator, the limitations check,
the schema's outer shape against v2's audit.schema.json, and the agent file the prompt loads."""

import copy
import json
from pathlib import Path

import pytest

from sre_harness import records
from sre_harness.spec import audit

REPO_ROOT = Path(__file__).resolve().parents[5]
V2_AUDIT = (
    REPO_ROOT
    / "plugins"
    / "toshipon-mode"
    / "skills"
    / "autonomous-improvement"
    / "schemas"
    / "audit.schema.json"
)
AGENT = REPO_ROOT / "plugins" / "sre-harness" / "agents" / "spec-auditor.md"

INDEX = {
    "FD-0001": "retention_days 400 -> 365 PR #101: retention_days 400 -> 365",
    "REQ-0002": "API は高速に応答すること。",
    "DEC-0001": "ログは JSON Lines で出力する",
    "TL-0001": "supersedes: [ADR-0003]",
    "PT-0001": "docs/requirements.md",
    "DOC-0010": "要求定義（合成） API は高速に応答すること。",
    "DOC-0010#S3": "REQ-0002: API の応答 API は高速に応答すること。",
}


def finding(number=1, severity="medium", category="verifiability", evidence=None, **extra):
    value = {
        "id": f"F{number}",
        "severity": severity,
        "category": category,
        "claim": f"Requirement claim number {number} has no measurable acceptance criterion.",
        "evidence": evidence if evidence is not None else ["REQ-0002"],
        "next_experiment": "Write a measurable criterion in the source section.",
    }
    value.update(extra)
    return value


# Axis status: one rule, table-driven -------------------------------------------------------

STATUS_TABLE = [
    # (severities of the axis's kept findings, assessed, a finding of the axis was rejected)
    (("critical",), True, False, "fail"),
    (("high",), True, False, "fail"),
    (("medium",), True, False, "concerns"),
    (("low",), True, False, "concerns"),
    (("low", "high"), True, False, "fail"),
    (("medium", "low"), True, False, "concerns"),
    (("critical", "medium"), True, False, "fail"),
    ((), True, False, "pass"),
    ((), False, False, "not_assessed"),
    ((), True, True, "not_assessed"),  # every finding of the axis was rejected
    (("low",), True, True, "concerns"),
    (("high",), False, False, "fail"),  # a finding outranks a not_assessed claim
]


@pytest.mark.parametrize("axis", audit.AXES)
@pytest.mark.parametrize(("severities", "assessed", "rejected", "expected"), STATUS_TABLE)
def test_axis_status_follows_the_severities_by_rule(axis, severities, assessed, rejected, expected):
    kept = [finding(number, severity, axis) for number, severity in enumerate(severities, start=1)]
    rejected_axes = {axis} if rejected else set()
    not_assessed = set() if assessed else {axis}
    axes = audit.axes(kept, not_assessed, rejected_axes, completed=True)
    assert axes[axis]["status"] == expected
    assert axes[axis]["finding_ids"] == [item["id"] for item in kept]
    others = [name for name in audit.AXES if name != axis]
    for name in others:
        assert axes[name]["finding_ids"] == []
        assert axes[name]["status"] == "pass"


def test_an_audit_that_did_not_complete_assesses_nothing():
    axes = audit.axes([], set(), set(), completed=False)
    assert {value["status"] for value in axes.values()} == {"not_assessed"}
    assert list(axes) == list(audit.AXES)


def test_the_eight_axes_are_the_owners_list():
    assert audit.AXES == (
        "coverage",
        "correctness",
        "consistency",
        "traceability",
        "verifiability",
        "clarity",
        "feasibility",
        "operability",
    )
    assert audit.BLOCKING == ("critical", "high")


# Evidence validator ------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        "REQ-0002",
        "FD-0001",
        "DEC-0001",
        "TL-0001",
        "PT-0001",
        "DOC-0010",
        "DOC-0010#S3",
        "REQ-0002: API は高速に応答すること。",
        "REQ-0002: 「高速に応答」",
        "DOC-0010#S3:   API   は高速に応答すること。",
        "FD-0001: retention_days 400 -> 365",
    ],
)
def test_evidence_that_names_an_existing_id_and_quotes_it_is_kept(entry):
    assert audit.check_finding(finding(evidence=[entry]), INDEX) == []


@pytest.mark.parametrize(
    ("entry", "reason"),
    [
        ("REQ-9999", "unknown_id"),
        ("DOC-0010#S9", "unknown_id"),
        ("REQ-0002: API はいつも 100 ms で応答する。", "quote_not_found"),
        ("DEC-0001: ログは JSON Lines で出力する", None),
        ("the requirements are vague", "evidence_format"),
        ("see REQ-0002", "evidence_format"),
        ("https://example.invalid/REQ-0002", "evidence_format"),
    ],
)
def test_evidence_that_cannot_be_checked_is_a_reason(entry, reason):
    reasons = audit.check_finding(finding(evidence=[entry]), INDEX)
    if reason is None:
        assert reasons == []
    else:
        assert len(reasons) == 1 and reasons[0].startswith(reason)


def test_one_bad_entry_among_good_ones_is_still_a_reason():
    reasons = audit.check_finding(finding(evidence=["REQ-0002", "REQ-0404"]), INDEX)
    assert reasons == ["unknown_id: evidence[1] REQ-0404"]


def test_the_index_holds_every_kind_of_id_the_audit_may_cite():
    data = {
        "findings": [
            {
                "id": "FD-0001",
                "explanation": "two PRs disagree",
                "entities": ["REQ-0001"],
                "evidence": [{"id": "E1", "ref": "abc:docs/x.md#L1", "note": "PR #1: a -> b"}],
            }
        ],
        "decisions": [{"id": "DEC-0001", "title": "t", "content": "c", "rationale": "r"}],
        "requirements": [
            {
                "id": "REQ-0001",
                "statement": "s",
                "rationale": "",
                "acceptance_criteria": [{"id": "AC1", "given": "g", "when": "w", "then": "t"}],
                "ambiguity": [{"kind": "vague_term", "span": "高速"}],
            }
        ],
        "trace": [
            {
                "id": "TL-0001",
                "from": {"type": "decision", "id": "DEC-0001"},
                "to": {"type": "requirement", "id": "REQ-0001"},
                "evidence": {"ref": "requirements.json#REQ-0001", "quote": "q"},
            }
        ],
        "patches": [{"id": "PT-0001", "finding_id": "FD-0001", "paths": ["docs/x.md"]}],
        "documents": [
            {
                "id": "DOC-0001",
                "title": "T",
                "sections": [{"id": "S1", "heading": "H", "text": "x"}],
            }
        ],
    }
    index = audit.build_index(data)
    assert set(index) == {"FD-0001", "DEC-0001", "REQ-0001", "TL-0001", "PT-0001", "DOC-0001"} | {
        "DOC-0001#S1"
    }
    assert "PR #1: a -> b" in index["FD-0001"] and "高速" in index["REQ-0001"]
    assert "docs/x.md" in index["PT-0001"] and "q" in index["TL-0001"]


# Limitations: no severe concern parked outside the findings --------------------------------


@pytest.mark.parametrize(
    "limitation",
    [
        "The production environment and its live configuration were not inspected.",
        "実環境と、DATA に無いリポジトリのファイルは見ていない。",
        "Conversations that name no id were not read.",
        "IaC の plan は DATA に無いので確かめていない。",
    ],
)
def test_an_ordinary_limitation_passes(limitation):
    assert audit.check_limitations([limitation], [finding()]) == []


@pytest.mark.parametrize(
    ("limitation", "code"),
    [
        ("A critical gap in retention may exist.", "severity_word"),
        ("There is a high chance the retention values conflict.", "severity_word"),
        ("保持期間に重大な矛盾がある可能性がある。", "severity_word"),
        ("致命的な欠落があるかもしれない。", "severity_word"),
        ("A blocker may remain in the export.", "severity_word"),
        ("REQ-0005 might not be tested anywhere.", "names_id"),
        ("DOC-0010#S3 was only skimmed.", "names_id"),
        ("F1 may be understated.", "names_finding"),
        (
            "Note: Requirement claim number 1 has no measurable acceptance criterion.",
            "repeats_claim",
        ),
    ],
)
def test_a_severe_or_specific_concern_in_limitations_is_refused(limitation, code):
    problems = audit.check_limitations([limitation], [finding()])
    assert problems and problems[0].startswith(f"limitations[0] {code}")


def test_a_limitation_may_name_an_id_that_a_finding_already_cites():
    kept = [finding(evidence=["REQ-0005"])]
    assert audit.check_limitations(["REQ-0005 was checked against the export only."], kept) == []


def test_a_limitation_that_names_an_id_with_no_finding_is_refused_even_without_findings():
    assert audit.check_limitations(["REQ-0005 was not examined."], []) == [
        "limitations[0] names_id: REQ-0005 is named but no finding cites it"
    ]


# Schemas -----------------------------------------------------------------------------------

_LOOSE = {"description", "maxLength", "maxItems", "title"}


def _resolve(schema, node):
    if isinstance(node, dict) and "$ref" in node and node["$ref"].startswith("#/$defs/"):
        return _resolve(schema, schema["$defs"][node["$ref"].split("/")[-1]])
    return node


def _shape(node):
    """The schema without descriptions and size caps: what a value must look like."""
    if isinstance(node, dict):
        return {key: _shape(value) for key, value in node.items() if key not in _LOOSE}
    if isinstance(node, list):
        return [_shape(item) for item in node]
    return node


def test_audit_json_findings_and_limitations_have_the_v2_outer_shape():
    v2 = json.loads(V2_AUDIT.read_text(encoding="utf-8"))
    ours = records.schema("spec-audit")
    v2_finding = _shape(v2["properties"]["findings"]["items"])
    our_finding = _shape(_resolve(ours, ours["properties"]["findings"]["items"]))
    # The one intended difference: category is the eight axes, not v2's eleven kinds.
    assert our_finding["properties"]["category"] == {"enum": list(audit.AXES)}
    assert v2_finding["properties"]["category"]["enum"] != list(audit.AXES)
    for value in (v2_finding, our_finding):
        value["properties"].pop("category")
    assert our_finding == v2_finding
    assert ours["properties"]["findings"]["type"] == v2["properties"]["findings"]["type"]
    assert _shape(ours["properties"]["limitations"]) == _shape(v2["properties"]["limitations"])
    assert {"findings", "limitations"} <= set(ours["required"])


def test_the_model_schema_is_self_contained_and_holds_the_same_finding():
    model = records.schema("llm-spec-audit")
    assert "$ref" not in json.dumps(model)
    ours = records.schema("spec-audit")
    assert model["properties"]["findings"]["items"] == _resolve(
        ours, ours["properties"]["findings"]["items"]
    )
    # Nothing in the model's answer can carry a status, a score or a verdict.
    assert set(model["properties"]) == {"findings", "limitations", "not_assessed"}
    assert model["additionalProperties"] is False


def test_an_answer_with_a_verdict_breaks_the_model_schema():
    answer = {"findings": [], "limitations": [], "not_assessed": [], "verdict": "pass"}
    assert records.violations("llm-spec-audit", answer)
    scored = {"findings": [finding(score=0.9)], "limitations": [], "not_assessed": []}
    assert records.violations("llm-spec-audit", scored)


def test_the_impact_report_audit_is_the_view_with_every_axis():
    view = {
        "ref": None,
        "status": "not_run",
        "axes": audit.axes([], set(), set(), completed=False),
        "blocking": [],
    }
    assert records.violations("spec-audit", view, "report_view") == []
    partial = copy.deepcopy(view)
    partial["axes"].pop("operability")
    assert records.violations("spec-audit", partial, "report_view")
    scored = copy.deepcopy(view)
    scored["axes"]["coverage"]["score"] = 3
    assert records.violations("spec-audit", scored, "report_view")


def test_the_call_record_knows_the_audit_stage():
    schema = records.schema("llm-call")
    assert "audit" in schema["properties"]["stage"]["enum"]


# The agent file is the prompt -------------------------------------------------------------


def test_the_agent_has_read_only_tools_and_the_eight_axes():
    text = AGENT.read_text(encoding="utf-8")
    head, _, body = text.partition("\n---\n")
    assert head.startswith("---\nname: spec-auditor\n")
    assert 'tools: ["Read", "Glob", "Grep"]' in head
    for axis in audit.AXES:
        assert f"`{axis}`" in body
    for word in (
        "網羅性",
        "正確性",
        "整合性",
        "追跡性",
        "検証可能性",
        "明確性",
        "実現可能性",
        "運用性",
    ):
        assert word in body
    assert "limitations" in body and "編集しない" in body


def test_the_instructions_load_the_agent_body_without_its_front_matter():
    text = audit.instructions()
    assert "name: spec-auditor" not in text
    assert "## 観点" in text and "limitations の規則" in text
    assert "F1" in text and "REQ-0001" in text


def test_a_retry_names_the_problems_in_the_instructions():
    text = audit.instructions(["limitations[0] severity_word"])
    assert "limitations[0] severity_word" in text
    assert audit.instructions() not in ("", text)
