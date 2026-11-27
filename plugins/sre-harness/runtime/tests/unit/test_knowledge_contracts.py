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


# knowledge context: one search from a task-context file ---------------------------------------

INDEXED = "2026-11-27T00:00:00Z"
SEARCHED = "2026-11-27T06:00:00Z"


@pytest.fixture
def corpus(home, tmp_path, monkeypatch):
    from sre_harness.knowledge import cli as knowledge_cli
    from sre_harness.knowledge import ingest

    monkeypatch.setattr(knowledge_cli, "clock", lambda: SEARCHED)
    root = state_root(home)
    scenario = knowledge_corpus.scenario(50)
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    ingest.update(root, build("file", {"input": path}), now=INDEXED)
    return home, scenario


def task_file(tmp_path, task, workspace="ws-a", name="task.json", **changes):
    value = {
        "schema_version": 1,
        "task_id": task["task_id"],
        "workspace_id": workspace,
        "principal": task["principal"],
        "project_id": task["project_id"],
        "component_id": task["component_id"],
        "repo": None,
        "base_sha": None,
        "changed_paths": task["paths"],
        "resource_types": task["resource_types"],
        **changes,
    }
    path = tmp_path / name
    path.write_text(json.dumps(value, ensure_ascii=False))
    return path


def cli_json(env, *argv):
    import io

    from sre_harness import cli

    out = io.StringIO()
    code = cli.main(list(argv), env=env, out=out)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


def context(env, path, *extra):
    return cli_json(env, "knowledge", "context", "--task-file", str(path), *extra)


def search_for(env, task, *extra):
    argv = ["knowledge", "search", "--principal", task["principal"]]
    argv += ["--project", task["project_id"], "--component", task["component_id"]]
    argv += ["--task", task["task_id"]]
    for path in task["paths"]:
        argv += ["--path", path]
    for kind in task["resource_types"]:
        argv += ["--resource-type", kind]
    return cli_json(env, *argv, *extra)


def without_task_total(result):
    return {**result, "budget": {**result["budget"], "task": None}}


def test_context_is_the_search_its_task_file_describes(corpus, tmp_path):
    env, scenario = corpus
    task = scenario.tasks[1]
    code, found = context(env, task_file(tmp_path, task), "--query", task["query"])
    assert code == 0
    assert problems("knowledge-search-result", found) == []
    assert found["records"]
    code, searched = search_for(env, task, "--query", task["query"])
    assert code == 0
    assert without_task_total(found) == without_task_total(searched)


def test_context_takes_expansion_words_as_search_does(corpus, tmp_path):
    env, scenario = corpus
    task = scenario.tasks[4]
    words = ["--expand", task["literal_query"], "--expand", "バックアップ"]
    code, found = context(env, task_file(tmp_path, task), "--query", task["query"], *words)
    assert code == 0 and problems("knowledge-search-result", found) == []
    assert found["retrieval"]["expansion"]["source"] == "caller"
    code, searched = search_for(env, task, "--query", task["query"], *words)
    assert code == 0
    assert without_task_total(found) == without_task_total(searched)


def test_context_counts_against_the_task_as_search_does(corpus, tmp_path):
    env, scenario = corpus
    task = scenario.tasks[1]
    _, first = context(env, task_file(tmp_path, task))
    _, second = context(env, task_file(tmp_path, task))
    used = first["budget"]["used_tokens"]
    assert first["budget"]["task"] == {
        "id": task["task_id"],
        "limit": 6000,
        "used_before": 0,
        "used_after": used,
    }
    assert second["budget"]["task"]["used_before"] == used
    _, searched = search_for(env, task)
    assert searched["budget"]["task"]["used_before"] == 2 * used


def test_context_takes_the_task_budget_extension_as_search_does(corpus, tmp_path):
    env, scenario = corpus
    path = task_file(tmp_path, scenario.tasks[1])
    code, found = context(env, path, "--extend-task-budget", "100", "--reason", "長い作業")
    assert code == 0 and found["budget"]["task"]["limit"] == 6100
    assert context(env, path, "--reason", "理由だけ")[0] == 2


def test_context_returns_only_the_records_of_the_task_project(corpus, tmp_path):
    env, scenario = corpus
    limited = next(t for t in scenario.tasks if t["principal"] == "workspace-b")
    _, found = context(env, task_file(tmp_path, limited), "--max-tokens", "100000")
    assert {r["id"] for r in found["records"]} == set(limited["applicable"])
    for shown in found["records"]:
        assert shown["scope"].get("project_id") in (None, limited["project_id"])


def test_context_stops_when_the_mandatory_records_do_not_fit(corpus, tmp_path):
    env, scenario = corpus
    overflowing = scenario.tasks[0]
    code, found = context(env, task_file(tmp_path, overflowing))
    assert code == 1 and found["status"] == "mandatory_overflow" and found["records"] == []


def test_context_with_an_unknown_principal_is_refused(corpus, tmp_path):
    env, scenario = corpus
    path = task_file(tmp_path, scenario.tasks[1], principal="workspace-z")
    assert context(env, path) == (3, None)


@pytest.mark.parametrize(
    "written",
    [
        "{not json",
        json.dumps({"schema_version": 1}),
        json.dumps([1, 2]),
        "﻿{}",
    ],
)
def test_a_task_file_outside_the_contract_is_an_input_error(corpus, tmp_path, written):
    env, _ = corpus
    path = tmp_path / "task.json"
    path.write_text(written)
    assert context(env, path) == (2, None)


def test_a_missing_or_oversized_task_file_is_an_input_error(corpus, tmp_path):
    env, scenario = corpus
    assert context(env, tmp_path / "absent.json") == (2, None)
    huge = task_file(tmp_path, scenario.tasks[1], changed_paths=[f"p/{n:06d}" for n in range(1000)])
    huge.write_text(huge.read_text() + " " * (256 * 1024))
    assert context(env, huge) == (2, None)


def test_a_task_file_is_only_read(corpus, tmp_path):
    env, scenario = corpus
    path = task_file(tmp_path, scenario.tasks[1])
    before = (path.read_bytes(), path.stat().st_mtime_ns)
    context(env, path)
    assert (path.read_bytes(), path.stat().st_mtime_ns) == before
