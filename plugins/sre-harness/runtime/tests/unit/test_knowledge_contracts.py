"""The three contracts the knowledge index offers a task runner, a decision queue and a source
adapter (`task-context`, `decision-request` / `decision-response`, `source-adapter`), and what
the harness itself hands over through them. Everything is synthetic."""

import copy
import dataclasses
import json

import pytest

from examples.synthetic import knowledge_corpus
from examples.synthetic import review_registry as rr
from sre_harness import paths, records
from sre_harness.knowledge.adapters import build
from tests.knowledge_helpers import spec_decision, spec_requirement, state_root, write_spec_run

SHA = "a" * 64
GIT = "b" * 40

TASK = {
    "schema_version": 1,
    "task_id": "TASK-0001",
    "workspace_id": "ws-a",
    "principal": "workspace-a",
    "project_id": "project-alpha",
    "component_id": "queue",
    "repo": "example-org/alpha",
    "base_sha": GIT,
    "changed_paths": ["infra/queue/main.tf"],
    "resource_types": ["queue"],
}

REQUEST = {
    "schema_version": 1,
    "id": "DR-0123456789ab",
    "task_id": "TASK-0001",
    "workspace_id": "ws-a",
    "principal": "workspace-a",
    "question": "キューの保持期間を 14 日から 30 日に延ばしてよいか",
    "options": ["延ばす", "延ばさない"],
    "evidence_ids": ["KN-0123456789ab"],
    "risk": "medium",
    "status": "open",
    "revision": {"generation": 3, "content_hashes": {"KN-0123456789ab": SHA}, "head_sha": GIT},
    "dedupe_key": SHA,
    "created_at": "2026-11-27T00:00:00Z",
}

RESPONSE = {
    "schema_version": 1,
    "request_id": "DR-0123456789ab",
    "option": "延ばさない",
    "responder": "alice",
    "answered_at": "2026-11-27T01:00:00Z",
    "reuse": {"scope": "task", "until": None},
}


def problems(name, value, definition=None):
    return records.violations(name, value, definition)


# task-context ---------------------------------------------------------------------------------


def test_the_task_context_example_of_the_design_conforms():
    assert problems("task-context", TASK) == []


def test_a_task_context_may_name_no_component_repo_or_base():
    value = {**TASK, "component_id": None, "repo": None, "base_sha": None}
    assert problems("task-context", value) == []


@pytest.mark.parametrize(
    "task_id",
    ["", "TASK 0001", "TASK/0001", "../x", "-TASK", "T" * 65, "チケット-1"],
)
def test_a_task_id_is_only_checked_for_length_and_characters(task_id):
    assert problems("task-context", {**TASK, "task_id": task_id}) != []


@pytest.mark.parametrize("task_id", ["TASK-0001", "x", "PROJ-12.a_b", "0" * 64])
def test_any_task_id_of_the_name_shape_is_taken_as_it_is(task_id):
    assert problems("task-context", {**TASK, "task_id": task_id}) == []


@pytest.mark.parametrize(
    "path",
    ["../etc/passwd", "infra/../../x", "/abs/path", "a\\b", "infra/\x00x", "", "a/./b", "."],
)
def test_a_changed_path_is_relative_and_never_climbs(path):
    assert problems("task-context", {**TASK, "changed_paths": [path]}) != []


@pytest.mark.parametrize(
    "change",
    [
        {"extra": 1},
        {"schema_version": 2},
        {"base_sha": "xyz"},
        {"resource_types": ["Queue"]},
        {"principal": None},
        {"project_id": None},
    ],
)
def test_a_task_context_outside_the_contract_does_not_conform(change):
    assert problems("task-context", {**TASK, **change}) != []


def test_every_task_context_field_is_required():
    for name in TASK:
        value = {k: v for k, v in TASK.items() if k != name}
        assert problems("task-context", value) != [], name


# decision-request / decision-response -----------------------------------------------------------


def test_the_decision_request_and_response_examples_conform():
    assert problems("decision-request", REQUEST) == []
    assert problems("decision-response", RESPONSE) == []


@pytest.mark.parametrize("status", ["open", "answered", "cancelled", "expired", "superseded"])
def test_a_request_has_one_of_five_statuses(status):
    assert problems("decision-request", {**REQUEST, "status": status}) == []


@pytest.mark.parametrize(
    "change",
    [
        {"status": "done"},
        {"risk": "critical"},
        {"options": ["only one"]},
        {"options": ["same", "same"]},
        {"evidence_ids": ["RG-0001"]},
        {"evidence_ids": []},
        {"question": "two\nlines"},
        {"question": ""},
        {"id": "DR-1"},
        {"dedupe_key": "x"},
        {"revision": {"generation": 1, "content_hashes": {}, "head_sha": None, "x": 1}},
        {"revision": {"generation": 1, "content_hashes": {"RG-0001": SHA}, "head_sha": None}},
    ],
)
def test_a_request_outside_the_contract_does_not_conform(change):
    assert problems("decision-request", {**REQUEST, **change}) != []


def test_a_request_may_carry_when_it_expires():
    value = {**REQUEST, "expires_at": "2026-11-28T00:00:00Z"}
    assert problems("decision-request", value) == []


def test_reuse_beyond_the_task_needs_an_end():
    beyond = copy.deepcopy(RESPONSE)
    beyond["reuse"] = {"scope": "project", "until": None}
    assert problems("decision-response", beyond) != []
    beyond["reuse"]["until"] = "2026-12-31T00:00:00Z"
    assert problems("decision-response", beyond) == []


@pytest.mark.parametrize(
    "change",
    [{"reuse": {"scope": "organization", "until": None}}, {"responder": ""}, {"option": ""}],
)
def test_a_response_outside_the_contract_does_not_conform(change):
    assert problems("decision-response", {**RESPONSE, **change}) != []


# source-adapter: the three adapters the harness ships hand over what the contract says -----------


def contract(changes):
    return json.loads(json.dumps(dataclasses.asdict(changes)))


def test_the_file_adapter_answers_within_the_source_adapter_contract(tmp_path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps(knowledge_corpus.scenario(50).corpus, ensure_ascii=False))
    answer = contract(build("file", {"input": corpus}).changes(None))
    assert answer["items"]
    assert problems("source-adapter", answer) == []


def test_the_review_registry_adapter_answers_within_the_contract(home):
    root = state_root(home)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    rr.write(root)
    answer = contract(build("review-registry", {"root": root}).changes(None))
    assert answer["items"] and answer["skipped"]
    assert problems("source-adapter", answer) == []


def test_the_spec_run_adapter_answers_within_the_contract(home):
    from tests.knowledge_helpers import write_config

    write_config(home)
    drafts = [spec_decision(1), spec_decision(2, status="proposed", basis="inferred")]
    run_id = write_spec_run(home, drafts, [spec_requirement(1)])
    adapter = build(
        "spec-run", {"root": state_root(home), "run": run_id, "project": "project-alpha"}
    )
    answer = contract(adapter.changes(None))
    assert answer["items"] and answer["skipped"]
    assert problems("source-adapter", answer) == []


def test_an_adapter_answer_outside_the_contract_does_not_conform():
    good = {"items": [], "tombstones": [], "cursor": None, "complete": True, "skipped": None}
    assert problems("source-adapter", good) == []
    for change in (
        {"complete": "yes"},
        {"items": [{"origin": {"kind": "x", "key": "y"}}]},
        {"tombstones": [{"origin": {"kind": "x", "key": "y"}, "reason": "lost"}]},
        {"skipped": [{"reason": "x"}]},
    ):
        assert problems("source-adapter", {**good, **change}) != [], change
    assert problems("source-adapter", {k: v for k, v in good.items() if k != "cursor"}) != []
