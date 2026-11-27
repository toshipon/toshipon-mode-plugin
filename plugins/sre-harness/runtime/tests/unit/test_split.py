"""`review split`: events, not merge dates, decide what generation may see."""

import json
from pathlib import Path

from examples.synthetic.github_responses import (
    SPLIT_T1,
    TIMEOUT_FINDING,
    TIMEOUT_PRS,
    evaluation_scenario,
)
from sre_harness.review import fix
from sre_harness.review import split as split_stage
from tests.helpers import generated_run, prepared_run, read_jsonl, run_cli, run_dir

LATE = "Late note: please also log the URL."


def late_events_fixture():
    """Evaluation PRs, plus a reply and a commit on a generation PR after T1."""
    fixture = evaluation_scenario()
    (pull,) = [pull for pull in fixture.pulls if pull.number == TIMEOUT_PRS[0]]
    thread = pull.threads[0]
    thread.comment("erin", LATE, created_at="2026-01-28T09:00:00Z", reply_to=thread.comments[0])
    pull.commit("A late follow-up", committed_at="2026-01-28T09:05:00Z")
    late = pull.thread(path="src/late.py", line=1, original_commit=pull.commits[0]["sha"])
    late.comment("bob", "A thread opened after the split.", created_at="2026-01-28T09:10:00Z")
    return fixture


def made_split(tmp_path, env, fixture, *extra):
    run_id = prepared_run(tmp_path, env, fixture)
    code, summary = run_cli(["review", "split", "--run", run_id, "--t1", SPLIT_T1, *extra], env)
    return run_id, code, summary


def test_the_split_follows_creation_time_and_records_what_generation_loses(tmp_path, home, clock):
    run_id, code, summary = made_split(tmp_path, home, late_events_fixture())
    assert code == 0
    split = json.loads((run_dir(home, run_id) / "split.json").read_text())
    assert split["evaluation"]["prs"] == [41, 42, 43]
    assert 41 not in split["generation"]["prs"] and 11 in split["generation"]["prs"]
    assert split["generation"]["dropped"] == {
        "comments": 2,  # the late reply and the late thread's comment
        "commits": 1,
        "undated_comments": 0,
        "undated_commits": 0,
    }
    assert summary["sha256"] == split["sha256"] == split_stage.seal(split)["sha256"]
    assert summary["dropped"] == split["generation"]["dropped"]


def test_a_gap_keeps_prs_on_neither_side(tmp_path, home, clock):
    run_id, code, summary = made_split(tmp_path, home, evaluation_scenario(), "--gap-days", "1")
    assert code == 0
    split = json.loads((run_dir(home, run_id) / "split.json").read_text())
    assert split["evaluation_from"] == "2026-01-28T00:00:00Z"
    # every evaluation PR opens on Jan 27, inside the one-day gap
    assert split["excluded"]["gap_prs"] == [41, 42, 43] and split["evaluation"]["prs"] == []


def test_analyze_with_the_split_uses_no_event_from_t1_on(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, late_events_fixture(), generate=False)
    directory = run_dir(home, run_id)
    analyze = json.loads((directory / "analyze.json").read_text())
    assert analyze["split_sha256"] == json.loads((directory / "split.json").read_text())["sha256"]
    # PR 41's thread (evaluation side) and the thread opened after t1; the late reply and the
    # late thread's comment
    assert analyze["split_left_out"]["threads"] == 2
    assert analyze["split_left_out"]["comments"] == 2
    threads = read_jsonl(directory / "threads-with-fix.jsonl")
    bodies = [c["body"] for t in threads for c in t["comments"]]
    assert LATE not in bodies and "A thread opened after the split." not in bodies
    assert {t["pr"]["number"] for t in threads}.isdisjoint({41, 42, 43})
    lessons = read_jsonl(directory / "lessons.jsonl")
    assert all(not set(lesson["prs"]) & {41, 42, 43} for lesson in lessons)
    (timeout,) = [lesson for lesson in lessons if lesson["problem"] == TIMEOUT_FINDING]
    assert TIMEOUT_PRS[0] in timeout["prs"]


def test_a_late_commit_takes_the_prs_patches_away_from_generation(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, late_events_fixture(), generate=False)
    split = split_stage.load(Path(split_path))
    _, contexts, left_out = split_stage.restrict(
        [], fix.pull_contexts(run_dir(home, run_id)), split
    )
    assert contexts[TIMEOUT_PRS[0]]["files"] == {} and left_out["patches"] >= 1
    assert contexts[TIMEOUT_PRS[1]]["files"]
    assert 41 not in contexts


def test_a_changed_or_foreign_split_is_refused(tmp_path, home, clock):
    run_id, code, summary = made_split(tmp_path, home, evaluation_scenario())
    path = run_dir(home, run_id) / "split.json"
    split = json.loads(path.read_text())
    split["evaluation"]["prs"] = [41]
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(split))
    argv = ["review", "analyze", "--run", run_id, "--backend", "stub", "--split"]
    code, _ = run_cli([*argv, str(tampered)], home)
    assert code == 2
    foreign = {**json.loads(path.read_text()), "repo": "example-org/other"}
    other = tmp_path / "other.json"
    other.write_text(json.dumps(split_stage.seal(foreign)))
    assert run_cli([*argv, str(other)], home)[0] == 2
    assert run_cli([*argv, str(tmp_path / "missing.json")], home)[0] == 2


def test_bad_split_options_are_input_errors(tmp_path, home, clock):
    run_id = prepared_run(tmp_path, home, evaluation_scenario())
    assert run_cli(["review", "split", "--run", run_id, "--t1", "2026-01-27"], home)[0] == 2
    argv = ["review", "split", "--run", run_id, "--t1", SPLIT_T1, "--gap-days", "-1"]
    assert run_cli(argv, home)[0] == 2


def test_a_listed_pr_is_on_neither_side_and_recorded(tmp_path, home, clock):
    run_id, code, summary = made_split(
        tmp_path, home, evaluation_scenario(), "--exclude-pr", "41", "--exclude-pr", "11"
    )
    assert code == 0
    path = run_dir(home, run_id) / "split.json"
    split = json.loads(path.read_text())
    assert split["excluded"]["listed_prs"] == [11, 41]
    assert 41 not in split["evaluation"]["prs"] and 11 not in split["generation"]["prs"]
    assert summary["excluded"]["listed_prs"] == 2
    assert split_stage.load(path)["sha256"] == split["sha256"]


def test_without_exclusions_the_split_has_no_listed_prs(tmp_path, home, clock):
    run_id, code, _ = made_split(tmp_path, home, evaluation_scenario())
    assert code == 0
    split = json.loads((run_dir(home, run_id) / "split.json").read_text())
    assert "listed_prs" not in split["excluded"]


def test_excluding_a_pr_the_run_did_not_collect_is_an_input_error(tmp_path, home, clock):
    run_id, code, _ = made_split(tmp_path, home, evaluation_scenario(), "--exclude-pr", "9999")
    assert code == 2
    assert not (run_dir(home, run_id) / "split.json").exists()
