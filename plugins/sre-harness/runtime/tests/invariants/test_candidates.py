"""Invariants of candidates and the registry, checked over whole synthetic runs.

1. Without --include-untrusted, no candidate rests only on external_only / bot_only lessons.
2. An approved registry version names an evaluation of exactly its artifact hash, and a record
   saying otherwise does not pass the schema.
"""

import json

import pytest

from examples.synthetic.github_responses import evaluation_scenario
from sre_harness import paths, records
from sre_harness.review import approve, candidates, registry
from tests.helpers import evaluate_cli, generated_run, read_jsonl, run_dir

UNTRUSTED = {"external_only", "bot_only"}


@pytest.mark.parametrize("injected", [False, True])
def test_no_candidate_rests_only_on_untrusted_lessons_without_the_flag(
    tmp_path, home, clock, injected
):
    run_id, _ = generated_run(tmp_path, home, evaluation_scenario(injected_exception=injected))
    directory = run_dir(home, run_id)
    lessons = {row["id"]: row for row in read_jsonl(directory / "lessons.jsonl")}
    assert UNTRUSTED <= {lesson["source_trust"] for lesson in lessons.values()}
    for candidate_id in candidates.ids(directory):
        record, _ = candidates.load(directory, candidate_id)
        trusts = {lessons[lesson]["source_trust"] for lesson in record["lesson_ids"]}
        assert not trusts <= UNTRUSTED, candidate_id
        assert record["untrusted_included"] is False


def test_every_approved_version_was_evaluated_with_its_own_hash(tmp_path, home, clock):
    run_id, split_path = generated_run(tmp_path, home, evaluation_scenario())
    assert evaluate_cli(home, run_id, split_path)[0] == 0
    directory, root = run_dir(home, run_id), paths.state_root(home)
    for candidate_id in candidates.ids(directory):
        record, _ = candidates.load(directory, candidate_id)
        prefix = record["artifact"]["content_sha256"][: approve.PREFIX_LENGTH]
        approve.approve(directory, root, candidate_id, "alice", "ok", lambda _: prefix, str)
    evaluation_file = directory / "evaluation.json"
    evaluation = json.loads(evaluation_file.read_text())
    evaluated = {(e["id"], e["content_sha256"]) for e in evaluation["candidates"]}
    approved = [
        version
        for versions in registry.entries(root).values()
        for version in versions
        if version["status"] == "approved"
    ]
    assert len(approved) == 2
    for version in approved:
        digest = version["candidate"]["content_sha256"]
        assert version["evaluation"]["content_sha256"] == digest
        assert (version["lineage"]["candidate_id"], digest) in evaluated
        assert version["evaluation"]["sha256"] == records.sha256(evaluation_file.read_bytes())
        assert version["evaluation"]["criteria_hash"] == evaluation["criteria_hash"]


def test_the_schema_refuses_an_approval_without_an_evaluation():
    version = {
        "schema_version": 1,
        "id": "RG-0001",
        "version": 1,
        "created_at": "2026-01-28T00:00:00Z",
        "status": "approved",
        "repo": "example-org/demo",
        "lineage": {"run_id": "run-20260128T000000Z-0123abcd", "candidate_id": "IC-0001"},
        "candidate": {
            "content_sha256": "a" * 64,
            "candidate_version": 1,
            "form": "prose",
            "target": "rule",
            "title": "t",
            "problem": "p",
            "artifact_file": "v0001.md",
        },
        "evaluation": None,
        "decision": {
            "reviewer": "alice",
            "reason": "ok",
            "decided_at": "2026-01-28T00:00:00Z",
            "via": "terminal_hash_prefix",
        },
        "previous_version": None,
        "activation": None,
    }
    assert records.violations("review-registry", version)
    command = {
        **version,
        "status": "rejected",
        "decision": {**version["decision"], "via": "command"},
    }
    assert records.violations("review-registry", command) == []
    approved_by_command = {
        **command,
        "status": "approved",
        "evaluation": {
            "ref": "runs/x/evaluation.json",
            "sha256": "b" * 64,
            "criteria_hash": "c" * 64,
            "status": "evaluated",
            "content_sha256": "a" * 64,
        },
    }
    assert records.violations("review-registry", approved_by_command)  # needs the terminal
