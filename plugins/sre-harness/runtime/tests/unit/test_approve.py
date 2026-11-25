"""`review approve / reject / retire`: a person at a terminal, an evaluated hash, a versioned registry."""

import io
import json
import re

import pytest

from examples.synthetic.github_responses import INJECTED_EXCEPTION, evaluation_scenario
from sre_harness import cli, paths, records
from sre_harness.errors import BoundaryError, InputError
from sre_harness.review import approve, candidates, registry
from sre_harness.review import cli as review_cli
from tests.helpers import evaluate_cli, generated_run, run_cli, run_dir

REASON = "Matches the team's HTTP policy."


@pytest.fixture
def evaluated(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario(injected_exception=True))
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    return run_id, split_path


def root(env):
    return paths.state_root(env)


def digest(env, run_id, candidate_id="IC-0001"):
    record, _ = candidates.load(run_dir(env, run_id), candidate_id)
    return record["artifact"]["content_sha256"]


def decide(env, run_id, typed=None, candidate_id="IC-0001", activate_as=None):
    shown = []
    typed = digest(env, run_id, candidate_id)[: approve.PREFIX_LENGTH] if typed is None else typed
    summary = approve.approve(
        run_dir(env, run_id),
        root(env),
        candidate_id,
        "alice",
        REASON,
        ask=lambda prompt: typed,
        show=shown.append,
        activate_as=activate_as,
    )
    return summary, "\n".join(shown)


def test_the_command_line_refuses_without_a_terminal(evaluated, home):
    run_id, _ = evaluated
    argv = ["review", "approve", "--run", run_id, "--candidate", "IC-0001"]
    code, _ = run_cli([*argv, "--reviewer", "alice", "--reason", REASON], home)
    assert code == 3
    assert registry.entries(root(home)) == {}
    record, _ = candidates.load(run_dir(home, run_id), "IC-0001")
    assert record["status"] == "evaluated"


def test_there_is_no_option_or_variable_that_skips_the_terminal_check(evaluated, home, capsys):
    run_id, _ = evaluated
    assert cli.main(["review", "approve", "--help"]) == 0
    options = set(re.findall(r"(?<![\w-])--[a-z][a-z-]*", capsys.readouterr().out))
    assert options == {"--help", "--run", "--candidate", "--reviewer", "--reason", "--activate-as"}
    env = {**home, "SRE_HARNESS_ASSUME_TTY": "1", "CI": "1", "TERM": "xterm-256color"}
    argv = ["review", "approve", "--run", run_id, "--candidate", "IC-0001"]
    assert run_cli([*argv, "--reviewer", "alice", "--reason", REASON], env)[0] == 3
    assert "TTY" not in review_cli.__dict__ and "ASSUME" not in review_cli.__dict__


def test_a_candidate_that_was_not_evaluated_is_refused(tmp_path, home, clock):
    run_id, _ = generated_run(tmp_path, home, evaluation_scenario())
    with pytest.raises(BoundaryError, match="has not been evaluated"):
        decide(home, run_id)
    assert registry.entries(root(home)) == {}


def test_a_wrong_hash_prefix_records_nothing(evaluated, home):
    run_id, _ = evaluated
    with pytest.raises(BoundaryError, match="typed hash does not match"):
        decide(home, run_id, typed="0" * approve.PREFIX_LENGTH)
    assert registry.entries(root(home)) == {}


def test_a_body_changed_after_evaluation_is_refused(evaluated, home):
    run_id, _ = evaluated
    body = run_dir(home, run_id) / "candidates" / "IC-0001" / "body.md"
    body.write_text(body.read_text() + "\nOne more line.\n")
    with pytest.raises(BoundaryError, match="changed after it was evaluated"):
        decide(home, run_id, typed="anything")


def test_approval_writes_version_one_and_the_screen_shows_sources_and_lint(evaluated, home):
    run_id, _ = evaluated
    summary, screen = decide(home, run_id)
    assert summary == {"candidate": "IC-0001", "registry_id": "RG-0001", "version": 1}
    (versions,) = registry.entries(root(home)).values()
    (version,) = versions
    assert version["status"] == "approved" and version["decision"]["via"] == "terminal_hash_prefix"
    assert version["candidate"]["content_sha256"] == digest(home, run_id)
    assert version["evaluation"]["content_sha256"] == digest(home, run_id)
    record, _ = candidates.load(run_dir(home, run_id), "IC-0001")
    assert record["status"] == "approved" and record["approval"]["registry_version"] == 1
    # The screen: every source with its author's type and association, quotes fenced, lint.
    assert "association none, login mallory" in screen
    assert "```text\n" + INJECTED_EXCEPTION + "\n```" in screen
    assert "association member" in screen and "author_type user" in screen
    assert "imperative_to_agent" in screen and "- url at" in screen
    assert "## Evaluation" in screen and "unevaluable" in screen
    folder = root(home) / "registry" / "RG-0001"
    assert sorted(p.name for p in folder.iterdir()) == ["v0001.json", "v0001.md"]
    assert oct(folder.stat().st_mode & 0o777) == "0o700"


def test_approving_the_same_content_twice_is_refused(evaluated, home):
    run_id, split_path = evaluated
    decide(home, run_id)
    with pytest.raises(BoundaryError):
        decide(home, run_id)  # the candidate is approved, not evaluated, now


def test_a_changed_body_needs_a_new_evaluation_and_becomes_version_two(evaluated, home):
    run_id, split_path = evaluated
    decide(home, run_id)
    body = run_dir(home, run_id) / "candidates" / "IC-0001" / "body.md"
    body.write_text(body.read_text().replace("Problem:", "Problem (edited):"))
    with pytest.raises(BoundaryError):
        decide(home, run_id, typed="x")
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    summary, _ = decide(home, run_id)
    assert summary == {"candidate": "IC-0001", "registry_id": "RG-0001", "version": 2}
    versions = registry.entries(root(home))["RG-0001"]
    assert [v["version"] for v in versions] == [1, 2] and versions[1]["previous_version"] == 1
    assert versions[0]["candidate"]["content_sha256"] != versions[1]["candidate"]["content_sha256"]
    record, _ = candidates.load(run_dir(home, run_id), "IC-0001")
    assert record["version"] == 2 and record["approval"]["registry_version"] == 2


def test_an_approved_rule_is_the_next_evaluations_baseline(evaluated, home):
    run_id, split_path = evaluated
    decide(home, run_id)
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    result = json.loads((run_dir(home, run_id) / "evaluation.json").read_text())
    assert [rule["id"] for rule in result["baseline_rules"]] == ["RG-0001"]
    assert result["configs"]["baseline"]["counts"]["true_positives"] == 1
    assert [entry["id"] for entry in result["candidates"]] == ["IC-0002"]


def test_activation_writes_a_diff_and_never_the_work_tree(evaluated, home, tmp_path):
    run_id, _ = evaluated
    summary, _ = decide(home, run_id, activate_as="docs/rules/http-timeout.md")
    diff = (root(home) / "registry" / "RG-0001" / "v0001.diff").read_text()
    assert summary["activation_diff"].endswith("v0001.diff")
    assert diff.startswith("--- /dev/null\n+++ b/docs/rules/http-timeout.md\n@@ -0,0 +1,")
    _, text = candidates.load(run_dir(home, run_id), "IC-0001")
    assert "".join(line[1:] + "\n" for line in diff.splitlines()[3:]) == text
    (version,) = registry.entries(root(home))["RG-0001"]
    assert version["activation"]["diff_sha256"] == records.sha256(diff)


@pytest.mark.parametrize(
    "path",
    [
        "../outside.md",
        "/etc/rules.md",
        ".git/hooks/pre-commit",
        ".github/workflows/x.yml",
        "./a.md",
    ],
)
def test_activation_paths_outside_the_repository_or_into_git_are_refused(evaluated, home, path):
    run_id, _ = evaluated
    with pytest.raises(BoundaryError, match="refused activation path"):
        decide(home, run_id, activate_as=path)
    assert registry.entries(root(home)) == {}


def test_reject_and_retire_are_recorded_as_versions(evaluated, home):
    run_id, _ = evaluated
    directory = run_dir(home, run_id)
    assert approve.reject(directory, root(home), "IC-0002", "bob", "Too vague.")["version"] == 1
    assert candidates.load(directory, "IC-0002")[0]["status"] == "rejected"
    decide(home, run_id)
    code, retired = run_cli(
        ["review", "retire", "--entry", "RG-0002", "--reviewer", "bob", "--reason", "Replaced."],
        home,
    )
    assert code == 0 and retired == {"registry_id": "RG-0002", "version": 2, "status": "retired"}
    statuses = {
        key: [v["status"] for v in versions]
        for key, versions in registry.entries(root(home)).items()
    }
    assert statuses == {"RG-0001": ["rejected"], "RG-0002": ["approved", "retired"]}
    assert candidates.load(directory, "IC-0001")[0]["status"] == "retired"
    assert registry.active_rules(root(home), "example-org/demo") == []
    with pytest.raises(InputError):
        approve.retire(root(home), "RG-0002", "bob", "Again.")


def test_reject_through_the_command_line(evaluated, home):
    run_id, _ = evaluated
    argv = ["review", "reject", "--run", run_id, "--candidate", "IC-0002"]
    code, summary = run_cli([*argv, "--reviewer", "bob", "--reason", "No."], home)
    assert code == 0 and summary["registry_id"] == "RG-0001"


@pytest.mark.parametrize("reviewer, reason", [("", REASON), ("alice", " "), ("a" * 201, REASON)])
def test_reviewer_and_reason_are_required(evaluated, home, reviewer, reason):
    run_id, _ = evaluated
    with pytest.raises(InputError):
        approve.approve(run_dir(home, run_id), root(home), "IC-0001", reviewer, reason, str, print)


def test_registry_files_are_never_replaced(evaluated, home):
    run_id, _ = evaluated
    decide(home, run_id)
    (version,) = registry.entries(root(home))["RG-0001"]
    with pytest.raises(InputError, match="already exists"):
        registry.write_version(root(home), version, "text")


def test_at_a_terminal_the_command_line_shows_the_screen_and_asks(evaluated, home, monkeypatch):
    # Simulates a person at a terminal by replacing the TTY probe in this test only; the
    # command line itself has no switch for it.
    run_id, _ = evaluated
    typed = digest(home, run_id)[: approve.PREFIX_LENGTH]
    asked = []
    monkeypatch.setattr(review_cli, "_is_terminal", lambda stream: True)
    monkeypatch.setattr("builtins.input", lambda prompt: asked.append(prompt) or typed)
    argv = ["review", "approve", "--run", run_id, "--candidate", "IC-0001"]
    out = io.StringIO()
    code = cli.main([*argv, "--reviewer", "alice", "--reason", REASON], env=home, out=out)
    assert code == 0 and asked and "content_sha256" in asked[0]
    text = out.getvalue()
    assert text.startswith("# Approve IC-0001") and '"registry_id": "RG-0001"' in text


def tamper(env, run_id, **fields):
    path = run_dir(env, run_id) / "candidates" / "IC-0001" / "candidate.json"
    record = json.loads(path.read_text())
    record.update(fields)
    records.write_json(path, record)


@pytest.mark.parametrize(
    "fields",
    [
        {"check": {"path_globs": ["**"], "added_line_pattern": "never", "message": "m"}},
        {"title": "A title nobody evaluated"},
    ],
)
def test_a_record_changed_after_evaluation_cannot_be_approved(evaluated, home, fields):
    run_id, _ = evaluated
    tamper(home, run_id, **fields)
    with pytest.raises(BoundaryError, match="does not match its artifact"):
        decide(home, run_id)
    assert registry.entries(root(home)) == {}


def test_the_command_line_also_refuses_a_changed_record(evaluated, home, monkeypatch):
    run_id, _ = evaluated
    tamper(home, run_id, title="A title nobody evaluated")
    typed = digest(home, run_id)[: approve.PREFIX_LENGTH]
    monkeypatch.setattr(review_cli, "_is_terminal", lambda stream: True)
    monkeypatch.setattr("builtins.input", lambda prompt: typed)
    argv = ["review", "approve", "--run", run_id, "--candidate", "IC-0001"]
    code = cli.main([*argv, "--reviewer", "alice", "--reason", REASON], env=home, out=io.StringIO())
    assert code == 3 and registry.entries(root(home)) == {}


def test_the_registry_records_only_what_the_artifact_hash_covers(evaluated, home):
    run_id, _ = evaluated
    _, screen = decide(home, run_id)
    (version,) = registry.entries(root(home))["RG-0001"]
    _, text = candidates.load(run_dir(home, run_id), "IC-0001")
    title, check, problems = candidates.parse(text)
    assert problems == []
    assert set(version["candidate"]) == {
        "content_sha256",
        "form",
        "title",
        "check",
        "artifact_file",
    }
    assert version["candidate"]["check"] == check and version["candidate"]["title"] == title
    assert version["candidate"]["content_sha256"] == records.sha256(text)
    stored = (root(home) / "registry" / "RG-0001" / "v0001.md").read_text()
    assert stored == text
    # The screen shows exactly what is recorded.
    recorded = json.dumps(
        {"form": "declarative_check", "title": title, "check": check},
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    )
    assert "## What is recorded" in screen and recorded in screen


def test_an_old_evaluation_cannot_approve_an_edited_check(evaluated, home):
    run_id, split_path = evaluated
    old = digest(home, run_id)
    body = run_dir(home, run_id) / "candidates" / "IC-0001" / "body.md"
    body.write_text(body.read_text().replace("client\\\\.get", "client\\\\.post"))
    with pytest.raises(BoundaryError, match="changed after it was evaluated"):
        decide(home, run_id, typed=old[: approve.PREFIX_LENGTH])
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    new = digest(home, run_id)
    assert new != old
    with pytest.raises(BoundaryError, match="typed hash does not match"):
        decide(home, run_id, typed=old[: approve.PREFIX_LENGTH])
    decide(home, run_id)
    (version,) = registry.entries(root(home))["RG-0001"]
    assert version["candidate"]["content_sha256"] == new
    assert version["candidate"]["check"]["added_line_pattern"] == r"client\.post\("


def test_a_changed_registry_record_is_refused_as_a_baseline(evaluated, home):
    run_id, split_path = evaluated
    decide(home, run_id)
    path = root(home) / "registry" / "RG-0001" / "v0001.json"
    version = json.loads(path.read_text())
    version["candidate"]["check"]["added_line_pattern"] = "never"
    path.write_text(records.canonical(version) + "\n")
    assert evaluate_cli(home, run_id, split_path)[0] == 3
