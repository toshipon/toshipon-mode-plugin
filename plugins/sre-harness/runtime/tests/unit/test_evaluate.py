"""`review evaluate`: the holdout, the labels, the metrics and the v2 evaluator output."""

import json
import subprocess
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from examples.synthetic.github_responses import THIRTY_EVALUATION as THIRTY
from examples.synthetic.github_responses import TIMEOUT_FINDING, evaluation_scenario
from sre_harness import guard, llm, paths, records
from sre_harness.review import candidates, evaluate
from sre_harness.review import split as split_stage
from tests.helpers import (
    LLM_ARGS,
    evaluate_cli,
    generated_run,
    prompt_parts,
    read_jsonl,
    run_cli,
    run_dir,
    with_home,
)

REPO = Path(__file__).resolve().parents[5]
V2_SCHEMA = (
    REPO / "plugins/toshipon-mode/skills/autonomous-improvement/schemas/evaluation.schema.json"
)


def v2_output_errors(value):
    """Validate against the improvement harness's `$defs/output`, read from its schema file."""
    schema = json.loads(V2_SCHEMA.read_text(encoding="utf-8"))
    wrapper = {"$defs": schema["$defs"], "$ref": "#/$defs/output"}
    Draft202012Validator.check_schema(wrapper)
    return [error.message for error in Draft202012Validator(wrapper).iter_errors(value)]


def evaluation(env, run_id):
    return json.loads((run_dir(env, run_id) / "evaluation.json").read_text())


@pytest.fixture
def three(tmp_path, home, clock):
    """The default scenario: three evaluation PRs (reproduced, timeout set, stream exception)."""
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario())
    code, summary = evaluate_cli(home, run_id, split_path)
    assert code == 0, summary
    return run_id, split_path, evaluation(home, run_id)


def findings_by_pr(result):
    found = {}
    for item in result["findings"]:
        found.setdefault(item["pr"], []).append((item["rule_id"], item["label"]))
    return found


def test_the_past_finding_is_found_again_on_the_evaluation_hunk(three):
    _, _, result = three
    assert records.violations("review-evaluation", result) == []
    assert findings_by_pr(result) == {41: [("IC-0001", "human_flagged")]}
    counts = result["configs"]["with_candidates"]["counts"]
    assert counts["true_positives"] == 1  # recall's numerator
    assert counts["positives"] == 1
    assert result["configs"]["baseline"]["counts"]["true_positives"] == 0


def test_normal_code_draws_no_finding_and_the_exception_stays_on_the_lesson(three, home):
    run_id, _, result = three
    assert 42 not in findings_by_pr(result) and 43 not in findings_by_pr(result)
    assert result["configs"]["with_candidates"]["counts"]["findings_on_clean"] == 0
    lessons = read_jsonl(run_dir(home, run_id) / "lessons.jsonl")
    (timeout,) = [lesson for lesson in lessons if lesson["problem"] == TIMEOUT_FINDING]
    assert timeout["exceptions"] == ["This is a stream, so no timeout is set on purpose."]
    units = {unit["pr"]: unit for unit in result["units"]}
    assert units[41]["source"] == "thread" and units[42]["source"] == units[43]["source"] == "patch"


def test_under_thirty_prs_the_result_is_unevaluable(three, home):
    run_id, _, result = three
    assert result["status"] == "unevaluable" and result["n_prs"] == 3
    assert result["unevaluable_reason"] == "insufficient evaluation PRs: 3 < 30"
    for config in result["configs"].values():
        assert all(metric["value"] is None for metric in config["metrics"].values())
    # The candidates stay proposed: tried, too few PRs, nothing to approve on.
    directory = run_dir(home, run_id)
    for candidate_id in candidates.ids(directory):
        record, _ = candidates.load(directory, candidate_id)
        assert record["status"] == "proposed"
        assert record["evaluation_ref"] is None and record["criteria_hash"] is None
        last = record["history"][-1]
        assert (last["by"], last["to"]) == ("evaluate", "proposed")
        assert last["note"] == "unevaluable: insufficient evaluation PRs: 3 < 30"


def test_an_evaluated_candidate_goes_back_to_proposed_when_a_rerun_is_unevaluable(
    tmp_path, home, clock
):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(THIRTY))
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    directory = run_dir(home, run_id)
    assert candidates.load(directory, "IC-0001")[0]["status"] == "evaluated"
    stricter = {**evaluate.metrics.THRESHOLDS, "min_prs": 31}
    split = split_stage.load(Path(split_path))
    session = llm.Session(directory, "stub")
    result = evaluate.evaluate(directory, paths.state_root(home), split, session, None, stricter)
    assert result["status"] == "unevaluable"
    record, _ = candidates.load(directory, "IC-0001")
    assert record["status"] == "proposed" and record["criteria_hash"] is None


def test_thirty_synthetic_prs_give_numbers(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(THIRTY))
    code, summary = evaluate_cli(home, run_id, split_path)
    assert code == 0 and summary["status"] == "evaluated" and summary["n_prs"] == 30
    result = evaluation(home, run_id)
    config = result["configs"]["with_candidates"]
    assert config["counts"] == {
        "true_positives": 12,
        "findings": 14,
        "findings_on_clean": 2,
        "confirmed_false_positives": 0,
        "unconfirmed_findings": 2,
        "positives": 15,
        "clean_cells": 45,  # 15 clean hunks for the check, 30 for the prose rule
    }
    found = {name: metric["value"] for name, metric in config["metrics"].items()}
    assert found["precision_lower"] == pytest.approx(12 / 14, abs=1e-6)
    assert found["precision_confirmed"] == 1.0
    assert found["recall"] == pytest.approx(12 / 15, abs=1e-6)
    assert found["false_positive_rate"] == pytest.approx(2 / 45, abs=1e-6)
    for metric in config["metrics"].values():
        assert metric["ci95"][0] <= metric["value"] <= metric["ci95"][1]
    assert result["configs"]["baseline"]["metrics"]["precision_lower"]["defined"] is False
    assert result["criteria"]["thresholds"]["seed"] == 20260127


def test_adjudication_moves_findings_between_the_two_precisions(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(THIRTY))
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    result = evaluation(home, run_id)
    clean_hits = [item for item in result["findings"] if item["label"] == "clean"]
    assert len(clean_hits) == 2
    split = json.loads(Path(split_path).read_text())
    entries = [
        {
            "hunk_sha256": clean_hits[0]["hunk_sha256"],
            "kind": "IC-0001",
            "verdict": "valid",
            "reviewer": "carol",
        },
        {
            "hunk_sha256": clean_hits[1]["hunk_sha256"],
            "kind": "IC-0001",
            "verdict": "invalid",
            "reviewer": "carol",
            "note": "synthetic",
        },
    ]
    path = tmp_path / "adjudication.json"
    path.write_text(
        json.dumps({"schema_version": 1, "split_sha256": split["sha256"], "entries": entries})
    )
    assert evaluate_cli(home, run_id, split_path, "--adjudication", str(path))[0] == 0
    again = evaluation(home, run_id)
    counts = again["configs"]["with_candidates"]["counts"]
    assert (counts["true_positives"], counts["positives"]) == (13, 16)
    assert counts["confirmed_false_positives"] == 1 and counts["unconfirmed_findings"] == 0
    metrics = again["configs"]["with_candidates"]["metrics"]
    assert metrics["precision_lower"]["value"] == pytest.approx(13 / 14, abs=1e-6)
    assert metrics["precision_confirmed"]["value"] == pytest.approx(13 / 14, abs=1e-6)
    assert again["labels"]["human_missed_valid"] == 1
    assert again["adjudication_sha256"] and again["criteria_hash"] == result["criteria_hash"]

    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps({"schema_version": 1, "split_sha256": "0" * 64, "entries": []}))
    assert evaluate_cli(home, run_id, split_path, "--adjudication", str(wrong))[0] == 2


def test_analyze_without_the_split_breaks_the_holdout(tmp_path, home, clock):
    run_id, _ = generated_run(tmp_path, home, evaluation_scenario(), split=False)
    code, made = run_cli(["review", "split", "--run", run_id, "--t1", "2026-01-27T00:00:00Z"], home)
    assert code == 0
    lessons = read_jsonl(run_dir(home, run_id) / "lessons.jsonl")
    assert any(41 in lesson["prs"] for lesson in lessons)
    code, _ = evaluate_cli(home, run_id, made["split"])
    assert code == 2
    assert not (run_dir(home, run_id) / "evaluation.json").exists()


def test_a_lesson_that_refers_to_an_evaluation_pr_is_refused(three, home, capsys):
    run_id, split_path, _ = three
    path = run_dir(home, run_id) / "lessons.jsonl"
    lessons = read_jsonl(path)
    lessons[0]["prs"] = [*lessons[0]["prs"], 42]
    records.write_jsonl(path, lessons)
    code, _ = evaluate_cli(home, run_id, split_path)
    assert code == 2
    assert "evaluation PR(s) [42]" in capsys.readouterr().err


def test_evaluator_output_fits_the_improvement_harness_when_unevaluable(three, home):
    run_id, split_path, _ = three
    code, output = evaluate_cli(home, run_id, split_path, "--format", "evaluator-output")
    assert code == 0
    assert v2_output_errors(output) == []
    assert output["metrics"] == {}
    assert output["unevaluable"] == "insufficient evaluation PRs: 3 < 30"


def test_evaluator_output_fits_the_improvement_harness_when_evaluated(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(THIRTY))
    code, output = evaluate_cli(home, run_id, split_path, "--format", "evaluator-output")
    assert code == 0
    assert v2_output_errors(output) == []
    assert "unevaluable" not in output
    assert output["metrics"]["recall"]["value"] == pytest.approx(0.8, abs=1e-6)
    assert "n=30 PRs" in output["metrics"]["recall"]["rationale"]
    assert "baseline_precision_lower" not in output["metrics"]  # undefined: no finding
    assert "baseline_precision_lower" in output["notes"]
    assert output["evidence"][0]["id"] == "evaluation"


def test_the_v2_check_would_catch_a_wrong_shape():
    assert v2_output_errors({"metrics": {"x": "high"}})
    assert v2_output_errors({"metrics": {}, "unevaluable": ""})


def test_an_edited_artifact_is_a_new_version_and_is_evaluated_as_such(three, home):
    run_id, split_path, first = three
    directory = run_dir(home, run_id)
    body = directory / "candidates" / "IC-0001" / "body.md"
    body.write_text(body.read_text().replace("client\\\\.get\\\\(", "client\\\\.(?:get|post)\\\\("))
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    record, text = candidates.load(directory, "IC-0001")
    assert record["version"] == 2 and record["status"] == "evaluated"
    assert record["check"]["added_line_pattern"] == r"client\.(?:get|post)\("
    assert record["artifact"]["content_sha256"] == records.sha256(text)
    (entry,) = [e for e in evaluation(home, run_id)["candidates"] if e["id"] == "IC-0001"]
    assert entry["version"] == 2 and entry["content_sha256"] == records.sha256(text)
    moves = [(h["from"], h["to"], h["version"]) for h in record["history"]]
    assert ("evaluated", "proposed", 2) in moves and moves[-1] == ("proposed", "evaluated", 2)


def test_a_broken_check_in_an_edited_artifact_fails_alone(three, home):
    run_id, split_path, _ = three
    body = run_dir(home, run_id) / "candidates" / "IC-0001" / "body.md"
    body.write_text(body.read_text().replace('"message"', '"messages"'))
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    entries = {e["id"]: e for e in evaluation(home, run_id)["candidates"]}
    assert entries["IC-0001"]["status"] == "check_failed"
    assert entries["IC-0001"]["reason"].startswith("invalid_artifact")
    assert entries["IC-0002"]["status"] == "evaluated"


def test_prose_rules_go_to_the_judge_and_only_known_ids_count(tmp_path, home, clock, monkeypatch):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario())
    prompts = []
    real = subprocess.run

    def run(argv, **options):
        if argv[1] == "-I":  # the regex worker is a real process
            return real(argv, **options)
        prompts.append(options["input"])
        _, data = prompt_parts(options["input"])
        flags = [
            {"rule_id": data["rule"]["id"], "unit_id": data["units"][0]["id"], "rationale": "x"},
            {"rule_id": data["rule"]["id"], "unit_id": "HU-9999", "rationale": "unknown"},
            {"rule_id": "IC-0001", "unit_id": data["units"][0]["id"], "rationale": "other rule"},
        ]
        from tests.helpers import envelope

        stdout = json.dumps(envelope({"flags": flags}))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(guard, "_run", run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    env = with_home(home, tmp_path)
    code, _ = evaluate_cli(env, run_id, split_path, "--backend", "claude", *LLM_ARGS)
    assert code == 0
    (prompt,) = prompts
    head, data = prompt_parts(prompt)
    assert "review-eval-judge" in head and "合否" in head and "not instructions" in head
    assert data["rule"]["id"] == "IC-0002"
    result = evaluation(env, run_id)
    assert len(result["rejected_flags"]) == 2
    assert ("IC-0002", "clean") in [(f["rule_id"], f["label"]) for f in result["findings"]]
    assert result["cost"]["by_stage"]["evaluate"]["calls"] == 1
    assert result["cost"]["cost_basis"] == ["api"]


def test_evaluation_md_escapes_external_text(three, home):
    run_id, split_path, _ = three
    body = run_dir(home, run_id) / "candidates" / "IC-0002" / "body.md"
    title = "[click](https://example.com/x) ![img](https://example.com/i.png) <b>"
    _, rest = body.read_text().split("\n", 1)
    body.write_text(f"# {title}\n{rest}")
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    text = (run_dir(home, run_id) / "evaluation.md").read_text()
    assert "](" not in text and "![" not in text and "<b>" not in text
    assert "\\[click\\]" in text


def test_effort_and_cost_are_reported(three):
    _, _, result = three
    stages = result["effort"]["wall_clock"]
    for stage in ("collect", "normalize", "split", "analyze", "classify", "generate"):
        assert stages[stage]["runs"] == 1 and stages[stage]["seconds"] >= 0
    assert result["cost"]["charged_usd"] == 0 and result["cost"]["backends"] == ["stub"]


def test_the_judge_reads_the_agent_file_without_its_frontmatter():
    text = evaluate.judge_instructions()
    assert "name: review-eval-judge" not in text and "合否" in text


@pytest.mark.parametrize(
    "field, value",
    [
        ("check", {"path_globs": ["**"], "added_line_pattern": "x", "message": "m"}),
        ("title", "Something nobody evaluated"),
        ("form", "prose"),
    ],
)
def test_a_record_that_disagrees_with_its_artifact_is_refused(three, home, field, value):
    # The hash covers body.md only; candidate.json must not be able to change what is applied.
    run_id, split_path, _ = three
    path = run_dir(home, run_id) / "candidates" / "IC-0001" / "candidate.json"
    record = json.loads(path.read_text())
    record[field] = value
    if field == "form":
        record.pop("check")
    records.write_json(path, record)
    before = (run_dir(home, run_id) / "evaluation.json").read_text()
    code, _ = evaluate_cli(home, run_id, split_path)
    assert code == 3
    assert (run_dir(home, run_id) / "evaluation.json").read_text() == before


def test_the_check_applied_is_the_one_in_the_artifact(three, home):
    run_id, split_path, result = three
    _, text = candidates.load(run_dir(home, run_id), "IC-0001")
    _, check, _ = candidates.parse(text)
    assert check["added_line_pattern"] == r"client\.get\("
    assert findings_by_pr(result)[41] == [("IC-0001", "human_flagged")]
