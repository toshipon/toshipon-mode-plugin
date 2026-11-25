"""`review classify`: targets by fixed rules, conflicts with existing rules as candidates only."""

import json
import os

import pytest

from examples.synthetic.github_responses import TIMEOUT_FINDING, review_learning_scenario
from sre_harness import records
from sre_harness.review import classify
from tests.helpers import LLM_ARGS, prepared_run, read_jsonl, run_cli, run_dir, with_home

RULES = """# HTTP

- Every outbound HTTP client call must set a timeout.
- HTTP clients may omit a timeout for streaming downloads.
- Use client B for outbound HTTP calls.

```
HTTP client timeout inside a code block is not a rule.
```
"""
NEW_POLICY = "Use client B instead of client A for outbound HTTP calls."


def write_rules(directory, text=RULES):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "http.md").write_text(text)
    (directory / "notes.txt").write_text("HTTP client timeout, but not Markdown.")
    return directory


def analyzed_run(tmp_path, env, injection=False, name="fx"):
    run_id = prepared_run(tmp_path, env, review_learning_scenario(injection=injection), name=name)
    code, _ = run_cli(["review", "analyze", "--run", run_id, "--backend", "stub"], env)
    assert code == 0
    return run_id


def classified(env, run_id, rules, *extra, backend="stub"):
    argv = ["review", "classify", "--run", run_id, "--existing-rules", str(rules)]
    code, summary = run_cli([*argv, "--backend", backend, *extra], env)
    directory = run_dir(env, run_id)
    lessons = {row["id"]: row for row in read_jsonl(directory / "lessons.jsonl")}
    outputs = {}
    for name in ("classified", "set-aside", "conflicts", "rejected-conflicts"):
        path = directory / f"{name}.jsonl"
        outputs[name] = read_jsonl(path) if path.exists() else None
    return code, summary, lessons, outputs


@pytest.fixture
def scenario(tmp_path, home, clock):
    run_id = analyzed_run(tmp_path, home)
    return classified(home, run_id, write_rules(tmp_path / "rules"))


def by_problem(lessons, rows):
    return {lessons[row["lesson_id"]]["problem"]: row for row in rows}


def test_candidates_get_a_target_and_the_rest_are_set_aside(scenario):
    code, summary, lessons, outputs = scenario
    assert code == 0
    targets = {
        problem: row["target"]
        for problem, row in by_problem(lessons, outputs["classified"]).items()
    }
    assert targets == {TIMEOUT_FINDING: "rule", NEW_POLICY: "knowledge"}
    aside = {
        problem: row["reasons"]
        for problem, row in by_problem(lessons, outputs["set-aside"]).items()
    }
    assert aside == {
        "Use client A for outbound HTTP calls.": ["superseded"],
        "Prefer tabs for indentation in this module.": ["external_only"],
        "Line exceeds 100 characters.": ["bot_only"],
        "Only here: rename tmp to buffer.": ["case_specific"],
    }
    assert summary == {
        "run_id": summary["run_id"],
        "classified": 2,
        "set_aside": 4,
        "conflicts": len(outputs["conflicts"]),
        "rejected_conflicts": 0,
        "rules": 3,
        "skipped_rule_files": [],
    }
    for row in outputs["classified"]:
        assert records.violations("review-classified", row) == []
    for row in outputs["set-aside"]:
        assert records.violations("review-classified", row, "set_aside") == []


def test_conflicts_with_existing_rules_are_listed_with_quotes(scenario):
    _, _, lessons, outputs = scenario
    found = {
        (lessons[row["lesson_id"]]["problem"], row["existing_ref"], row["kind"])
        for row in outputs["conflicts"]
    }
    assert (TIMEOUT_FINDING, "http.md#L3", "narrows") in found  # the stream exception
    assert (TIMEOUT_FINDING, "http.md#L4", "contradicts") in found
    assert (NEW_POLICY, "http.md#L5", "duplicates") in found
    lines = RULES.splitlines()
    for row in outputs["conflicts"]:
        assert records.violations("review-conflict", row) == []
        line = int(row["existing_ref"].rsplit("#L", 1)[1])
        assert row["quote"] in lines[line - 1]
        assert row["detector"] == "stub"
    assert not any("code block" in row["quote"] for row in outputs["conflicts"])


def test_conflicts_do_not_change_the_outcome(tmp_path, home, clock, scenario):
    code, _, _, with_rules = scenario
    run_id = analyzed_run(tmp_path, home, name="other")
    empty = tmp_path / "no-rules"
    empty.mkdir()
    other_code, _, _, without = classified(home, run_id, empty)
    assert code == other_code == 0
    assert with_rules["conflicts"] and without["conflicts"] == []

    def shape(outputs):
        return sorted((row["target"], row["rule"]) for row in outputs["classified"])

    assert shape(with_rules) == shape(without)


def test_prompt_injection_changes_no_classification(tmp_path, home, clock):
    rules = write_rules(tmp_path / "rules")

    def shape(injection, name):
        run_id = analyzed_run(tmp_path, home, injection=injection, name=name)
        _, _, lessons, outputs = classified(home, run_id, rules)
        return (
            sorted(
                (lessons[r["lesson_id"]]["problem"], r["target"]) for r in outputs["classified"]
            ),
            sorted(
                (lessons[r["lesson_id"]]["problem"], tuple(r["reasons"]))
                for r in outputs["set-aside"]
            ),
            sorted(
                (lessons[r["lesson_id"]]["problem"], r["existing_ref"], r["kind"])
                for r in outputs["conflicts"]
            ),
        )

    assert shape(True, "injected") == shape(False, "plain")


def test_backend_conflicts_are_checked_against_the_rule_text(tmp_path, home, clock, fake_claude):
    run_id = analyzed_run(tmp_path, home)
    lessons = {
        row["problem"]: row["id"] for row in read_jsonl(run_dir(home, run_id) / "lessons.jsonl")
    }
    timeout, old = lessons[TIMEOUT_FINDING], lessons["Use client A for outbound HTTP calls."]

    def conflict(lesson, ref, quote, kind="duplicates"):
        return {
            "lesson_id": lesson,
            "existing_ref": ref,
            "kind": kind,
            "basis": "Same point.",
            "quote": quote,
        }

    calls = fake_claude(
        {
            "conflicts": [
                conflict(timeout, "http.md#L3", "must set a timeout"),
                conflict(timeout, "http.md#L3", "must never time out"),
                conflict(timeout, "http.md#L99", "must set a timeout"),
                conflict(old, "http.md#L5", "Use client B"),  # set aside, not a candidate
                conflict("LS-9999", "http.md#L5", "Use client B"),
            ]
        }
    )
    env = with_home(home, tmp_path)
    code, summary, _, outputs = classified(
        env, run_id, write_rules(tmp_path / "rules"), *LLM_ARGS, backend="claude"
    )
    assert code == 0 and len(calls) == 1
    assert [
        (row["existing_ref"], row["quote"], row["detector"]) for row in outputs["conflicts"]
    ] == [("http.md#L3", "must set a timeout", "llm")]
    reasons = [row["reasons"][0].split(":")[0] for row in outputs["rejected-conflicts"]]
    assert reasons == ["quote_not_found", "unknown_rule", "not_a_candidate", "not_a_candidate"]
    assert summary["rejected_conflicts"] == 4
    assert [row["target"] for row in outputs["classified"]] == ["rule", "knowledge"]


# Reading existing rules -------------------------------------------------------------------


def test_symlinks_are_not_followed(tmp_path, home, clock):
    run_id = analyzed_run(tmp_path, home)
    rules = write_rules(tmp_path / "rules", "# Empty\n")
    outside = tmp_path / "outside.md"
    outside.write_text("- Every outbound HTTP client call must set a timeout.\n")
    os.symlink(outside, rules / "linked.md")
    os.symlink(tmp_path, rules / "linked-dir")
    code, summary, _, outputs = classified(home, run_id, rules)
    assert code == 0 and outputs["conflicts"] == []
    assert summary["skipped_rule_files"] == ["linked-dir", "linked.md"]


def test_a_symlinked_rules_directory_is_refused(tmp_path, home, clock):
    run_id = analyzed_run(tmp_path, home)
    os.symlink(write_rules(tmp_path / "rules"), tmp_path / "rules-link")
    code, _, _, outputs = classified(home, run_id, tmp_path / "rules-link")
    assert code == 3 and outputs["classified"] is None


def test_a_rule_file_over_the_cap_stops_instead_of_being_cut(tmp_path, home, clock, monkeypatch):
    monkeypatch.setattr(classify, "MAX_RULE_FILE_BYTES", 64)
    run_id = analyzed_run(tmp_path, home)
    code, _, _, outputs = classified(home, run_id, write_rules(tmp_path / "rules"))
    assert code == 2 and outputs["classified"] is None


@pytest.mark.parametrize("problem", ["missing_dir", "no_backend", "not_analyzed"])
def test_inputs_must_exist(tmp_path, home, clock, problem):
    if problem == "not_analyzed":
        run_id = prepared_run(tmp_path, home, review_learning_scenario())
    else:
        run_id = analyzed_run(tmp_path, home)
    rules = tmp_path / "missing" if problem == "missing_dir" else write_rules(tmp_path / "rules")
    argv = ["review", "classify", "--run", run_id, "--existing-rules", str(rules)]
    if problem != "no_backend":
        argv += ["--backend", "stub"]
    code, _ = run_cli(argv, home)
    assert code == 2


# The fixed target rules -------------------------------------------------------------------


def lesson(problem, confidence="candidate", target="src/app.py"):
    return {
        "target": target,
        "problem": problem,
        "fix_summary": "",
        "applies_when": [],
        "confidence": confidence,
    }


@pytest.mark.parametrize(
    "item, fixed, target",
    [
        (
            lesson("The security group is open to the world.", target="infra/main.tf"),
            False,
            "policy_as_code",
        ),
        (lesson("Terraform state bucket has no versioning."), False, "policy_as_code"),
        (lesson("Off-by-one bug in the pagination loop."), True, "regression_test"),
        (lesson("Off-by-one bug in the pagination loop."), False, "knowledge"),
        (lesson("The p95 latency of the search endpoint doubled."), False, "evaluation"),
        (lesson("Follow the release steps in order."), False, "skill"),
        (lesson("Name exported functions with a verb.", "recurring"), False, "rule"),
        (lesson("Name exported functions with a verb.", "strong"), False, "rule"),
        (lesson("Name exported functions with a verb."), False, "knowledge"),
    ],
)
def test_targets_follow_the_first_matching_rule(item, fixed, target):
    assert classify.target_for(item, fixed)[0] == target


def test_every_target_rule_names_a_known_target():
    names = [rule[0] for rule in classify.TARGET_RULES]
    assert len(names) == len(set(names))
    assert {rule[1] for rule in classify.TARGET_RULES} <= set(classify.TARGETS)
    assert json.loads(json.dumps(classify.TARGETS)) == list(classify.TARGETS)
