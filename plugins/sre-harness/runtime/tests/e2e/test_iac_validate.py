"""`iac validate` end to end through cli.main, on synthetic plans, evidence and deliveries."""

import argparse
import json

import pytest
from jsonschema import Draft202012Validator

from examples.synthetic import iac_scenarios as sc
from sre_harness import cli, records
from tests.iac_helpers import V2_SCHEMA, no_opa, opa_on_path, read, runs, validate  # noqa: F401


@pytest.fixture
def v2():
    schema = json.loads(V2_SCHEMA.read_text(encoding="utf-8"))
    return Draft202012Validator({**schema, "$ref": "#/$defs/output"})


# The command surface ------------------------------------------------------------------


def _subcommands(parser):
    (action,) = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    return action.choices


def test_the_iac_group_has_validate_and_nothing_else():
    iac = _subcommands(cli.build_parser())["iac"]
    assert set(_subcommands(iac)) == {"validate"}


@pytest.mark.parametrize("stage", ["deploy", "apply", "advance", "record", "approve", "run"])
def test_no_command_moves_a_change_forward(home, stage, capsys):
    assert cli.main(["iac", stage], env=home) == 2
    assert "invalid choice" in capsys.readouterr().err


# A valid delivery ---------------------------------------------------------------------


def test_a_valid_delivery_passes_and_without_opa_the_risk_is_unevaluable(home, tmp_path, no_opa):  # noqa: F811
    case = sc.write_case(tmp_path / "case")
    code, summary = validate(home, case)
    assert code == 0
    assert summary["status"] == "valid" and summary["violations"] == {}
    assert summary["risk"] == "unevaluable"
    run_id = summary["run_id"]
    check = read(home, run_id, "transition-check.json")
    assert check["status"] == "valid" and check["violations"] == []
    assert check["change_id"] == sc.CHANGE_ID and check["state"] == "deployed"
    assert check["transitions_checked"] == 7
    assert check["plan_sha256"] == sc.sha256_file(case["plan"])
    found = read(home, run_id, "risk.json")
    assert found["status"] == "unevaluable" and "opa" in found["unevaluable_reason"]
    assert records.violations("iac-delivery", check, "transition_check") == []
    assert records.violations("iac-delivery", found, "risk") == []


def test_with_opa_the_risk_of_the_plan_is_classified(home, tmp_path, opa_on_path):  # noqa: F811
    case = sc.write_case(tmp_path / "case")
    code, summary = validate(home, case)
    assert code == 0 and summary["risk"] == "high"
    found = read(home, summary["run_id"], "risk.json")
    assert found["status"] == "evaluated" and found["level"] == "high"
    levels = {item["address"]: item["level"] for item in found["changes"]}
    assert levels == sc.EXPECTED_RISK["high"]["changes"]


def test_a_plan_alone_is_classified_and_no_transition_is_checked(home, tmp_path, no_opa):  # noqa: F811
    plan = sc.write_plan(tmp_path / "plan.json", "low")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    code, summary = validate(home, plan=plan, evidence=evidence)
    assert code == 0 and summary["status"] == "not_checked"
    check = read(home, summary["run_id"], "transition-check.json")
    assert check["status"] == "not_checked" and check["change_id"] is None


def test_run_files_are_private(home, tmp_path, no_opa):  # noqa: F811
    code, summary = validate(home, sc.write_case(tmp_path / "case"))
    directory = tmp_path / "state" / "sre-harness" / "runs" / summary["run_id"]
    assert sorted(p.name for p in directory.iterdir()) == ["risk.json", "transition-check.json"]
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in directory.iterdir())


# Contract violations ------------------------------------------------------------------


@pytest.mark.parametrize("fault", sorted(sc.BROKEN))
def test_each_broken_delivery_fails_with_its_violation(home, tmp_path, no_opa, fault):  # noqa: F811
    case = sc.write_case(tmp_path / "case", fault)
    code, summary = validate(home, case)
    assert code == 1
    assert summary["status"] == "violations"
    assert sc.BROKEN[fault] in summary["violations"]
    check = read(home, summary["run_id"], "transition-check.json")
    assert sc.BROKEN[fault] in [item["code"] for item in check["violations"]]
    assert [item["id"] for item in check["violations"]] == [
        f"IV-{n:04d}" for n in range(1, len(check["violations"]) + 1)
    ]


def test_a_plan_other_than_the_delivered_one_is_a_violation(home, tmp_path, no_opa):  # noqa: F811
    case = sc.write_case(tmp_path / "case")
    other = sc.write_plan(tmp_path / "other.json", "low")
    code, summary = validate(home, case, plan=other)
    assert code == 1 and "plan_mismatch" in summary["violations"]


def test_a_delivery_out_of_schema_is_a_violation_not_a_crash(home, tmp_path, no_opa):  # noqa: F811
    case = sc.write_case(tmp_path / "case")
    case["delivery"].write_text(json.dumps({"change_id": "CHG-0001", "state": "applied"}))
    code, summary = validate(home, case)
    assert code == 1 and set(summary["violations"]) == {"schema"}


# Input errors: exit 2, no run -----------------------------------------------------------


@pytest.mark.parametrize(
    "breakage",
    ["missing_plan", "plan_not_json", "plan_shape", "missing_evidence", "delivery_not_json"],
)
def test_wrong_input_is_exit_2_and_creates_no_run(home, tmp_path, breakage):
    case = sc.write_case(tmp_path / "case")
    if breakage == "missing_plan":
        case["plan"].unlink()
    elif breakage == "plan_not_json":
        case["plan"].write_text("{not json")
    elif breakage == "plan_shape":
        case["plan"].write_text(json.dumps({"resource_changes": [{"address": 1}]}))
    elif breakage == "missing_evidence":
        case["evidence"] = tmp_path / "nowhere"
    else:
        case["delivery"].write_text("[")
    code, summary = validate(home, case)
    assert code == 2 and summary is None
    assert runs(home) == []


# The v2 evaluator adapter ---------------------------------------------------------------


def test_evaluator_output_fits_the_v2_schema_for_a_valid_delivery(home, tmp_path, no_opa, v2):  # noqa: F811
    code, output = validate(home, sc.write_case(tmp_path / "case"), "--format", "evaluator-output")
    assert code == 0
    assert list(v2.iter_errors(output)) == []
    assert output["metrics"]["contract_violations"] == 0
    assert output["metrics"]["transitions_checked"] == 7
    assert "risk_high" not in output["metrics"]
    assert "unevaluable" not in output
    assert "risk" in output["notes"] and "unevaluable" in output["notes"]


def test_evaluator_output_lists_each_violation_as_evidence(home, tmp_path, no_opa, v2):  # noqa: F811
    case = sc.write_case(tmp_path / "case", "approver_is_implementer")
    code, output = validate(home, case, "--format", "evaluator-output")
    assert code == 1
    assert list(v2.iter_errors(output)) == []
    assert output["metrics"]["contract_violations"] == 1
    assert {"id": "IV-0001", "ref": "transition-check.json#IV-0001"}.items() <= output["evidence"][
        0
    ].items()
    assert output["evidence"][0]["note"].startswith("approver_is_implementer")


def test_evaluator_output_is_unevaluable_when_nothing_could_be_checked(home, tmp_path, no_opa, v2):  # noqa: F811
    plan = sc.write_plan(tmp_path / "plan.json", "low")
    (tmp_path / "evidence").mkdir()
    code, output = validate(
        home, None, "--format", "evaluator-output", plan=plan, evidence=tmp_path / "evidence"
    )
    assert code == 0
    assert list(v2.iter_errors(output)) == []
    assert output["metrics"] == {} and "opa" in output["unevaluable"]


def test_evaluator_output_carries_the_risk_counts_with_opa(home, tmp_path, opa_on_path, v2):  # noqa: F811
    case = sc.write_case(tmp_path / "case")
    code, output = validate(home, case, "--format", "evaluator-output")
    assert code == 0
    assert list(v2.iter_errors(output)) == []
    assert output["metrics"]["risk_high"] == 5 and output["metrics"]["risk_medium"] == 1
    assert output["metrics"]["risk_changes_total"] == 6


# Input nested too deep to parse: exit 2, no run -----------------------------------------

DEEP = "[" * 200_000 + "]" * 200_000


@pytest.mark.parametrize("which", ["plan", "delivery"])
def test_json_nested_too_deep_is_exit_2_and_creates_no_run(home, tmp_path, which):
    case = sc.write_case(tmp_path / "case")
    if which == "plan":
        case["plan"].write_text('{"resource_changes": [], "x": ' + DEEP + "}")
    else:
        case["delivery"].write_text(DEEP)
    code, summary = validate(home, case)
    assert code == 2 and summary is None
    assert runs(home) == []


# A plan out of shape is an input error before opa sees it -------------------------------


@pytest.mark.parametrize(
    "resource_changes",
    [
        {"a": 1},
        "none",
        [{"address": "a.b", "type": "a", "change": {"actions": "delete"}}],
        [{"address": "a.b", "type": "a", "change": {"actions": []}}],
        [{"address": "a.b", "type": "a", "change": {"actions": ["delete", 1]}}],
        [{"address": "a.b", "type": "a", "change": {"actions": [""]}}],
        [{"address": "a.b", "type": "a", "change": {}}],
    ],
)
def test_a_plan_whose_actions_cannot_be_classified_is_exit_2(home, tmp_path, resource_changes):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"resource_changes": resource_changes}))
    (tmp_path / "evidence").mkdir()
    code, summary = validate(home, plan=plan, evidence=tmp_path / "evidence")
    assert code == 2 and summary is None
    assert runs(home) == []


# Whether --plan is the plan the delivery recorded ---------------------------------------


def test_a_delivery_with_no_plan_yet_says_the_plan_is_not_bound_to_it(home, tmp_path, no_opa):  # noqa: F811
    evidence = tmp_path / "case/evidence"
    delivery = sc.valid_delivery(evidence, until="implemented")
    path = tmp_path / "case/delivery.json"
    path.write_text(json.dumps(delivery))
    other = sc.write_plan(tmp_path / "other.json", "low")
    code, summary = validate(home, plan=other, evidence=evidence, delivery=path)
    assert code == 0 and summary["status"] == "valid"
    assert summary["bound_to_delivery"] is False
    assert read(home, summary["run_id"], "risk.json")["bound_to_delivery"] is False


def test_the_delivered_plan_is_bound_to_the_delivery(home, tmp_path, no_opa):  # noqa: F811
    code, summary = validate(home, sc.write_case(tmp_path / "case"))
    assert code == 0 and summary["bound_to_delivery"] is True
    assert read(home, summary["run_id"], "risk.json")["bound_to_delivery"] is True


def test_a_plan_alone_is_not_bound_to_any_delivery(home, tmp_path, no_opa):  # noqa: F811
    plan = sc.write_plan(tmp_path / "plan.json", "low")
    (tmp_path / "evidence").mkdir()
    code, summary = validate(home, plan=plan, evidence=tmp_path / "evidence")
    assert code == 0 and summary["bound_to_delivery"] is False
    assert read(home, summary["run_id"], "risk.json")["bound_to_delivery"] is False


def test_evaluator_output_says_when_the_plan_is_not_bound(home, tmp_path, no_opa, v2):  # noqa: F811
    evidence = tmp_path / "case/evidence"
    path = tmp_path / "case/delivery.json"
    path.write_text(json.dumps(sc.valid_delivery(evidence, until="implemented")))
    plan = sc.write_plan(tmp_path / "other.json", "low")
    code, output = validate(
        home, None, "--format", "evaluator-output", plan=plan, evidence=evidence, delivery=path
    )
    assert code == 0 and list(v2.iter_errors(output)) == []
    assert "not bound" in output["notes"]
