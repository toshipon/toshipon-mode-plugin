"""`sre-harness knowledge fetch` and the task total end to end, through `cli.main`, on the
synthetic corpus and its source texts written into the state root."""

import io
import json
import os
import shutil
import subprocess
import sys

import pytest

from examples.synthetic import knowledge_corpus as kc
from sre_harness import cli, paths, records
from sre_harness.knowledge import budget, ingest, model, untrusted
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge.adapters import build
from tests.invariants.test_boundaries import RUNTIME
from tests.knowledge_helpers import (
    bare_repository,
    git_evidence,
    item,
    registry_evidence,
    state_root,
    update,
    write_config,
    write_input,
)

INDEXED = "2026-11-27T00:00:00Z"
NOW = "2026-11-27T06:00:00Z"
SCHEMA = "knowledge-fetch-result"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    home = tmp_path_factory.mktemp("fetch")
    env = {"AI_HARNESS_HOME": str(home / "state")}
    root = paths.state_root(env)
    scenario = kc.scenario(300)
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    path = home / "corpus.json"
    path.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    ingest.update(root, build("file", {"input": path}), now=INDEXED)
    kc.write_sources(root, scenario)
    return env, scenario


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(knowledge_cli, "clock", lambda: NOW)


@pytest.fixture
def fresh(corpus):
    """The corpus with no task ledger yet."""
    env, scenario = corpus
    shutil.rmtree(paths.state_root(env) / "knowledge" / "tasks", ignore_errors=True)
    return env, scenario


def raw(env, stage, *argv):
    out = io.StringIO()
    code = cli.main(["knowledge", stage, *argv], env=env, out=out)
    return code, out.getvalue()


def run(env, *argv, schema=SCHEMA, stage="fetch"):
    code, text = raw(env, stage, *argv)
    value = json.loads(text) if text.strip() else None
    if value is not None:
        assert records.violations(schema, value) == [], value
    return code, value


def search(env, *argv):
    return run(env, *argv, schema="knowledge-search-result", stage="search")


def inner(envelope):
    return envelope[envelope.index("\n") + 1 : envelope.rindex("\n" + untrusted.CLOSE)]


def by_id(scenario):
    return {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]): i
        for i in scenario.corpus["items"]
    }


def task_search(task, *extra):
    return [
        "--principal",
        task["principal"],
        "--project",
        task["project_id"],
        "--component",
        task["component_id"],
        *[part for path in task["paths"] for part in ("--path", path)],
        *[part for kind in task["resource_types"] for part in ("--resource-type", kind)],
        *extra,
    ]


# Fetching what search returned ----------------------------------------------------------------


def test_what_search_returns_can_be_fetched_and_matches_its_hash(fresh):
    env, scenario = fresh
    traps = set(scenario.traps["stale_source"]) | set(scenario.traps["missing_source"])
    checked = 0
    for task in scenario.tasks[1:8]:
        code, found = search(env, *task_search(task))
        assert code == 0
        for shown in found["records"]:
            code, result = run(env, "--principal", task["principal"], "--id", shown["id"])
            if shown["id"] in traps:
                assert code == 1
                continue
            assert code == 0 and result["status"] == "ok", result
            (evidence,) = shown["evidence"]
            (fragment,) = result["fragments"]
            assert fragment["locator"] == evidence["locator"]
            assert fragment["content_sha256"] == evidence["content_sha256"]
            assert fragment["hash_match"] is True
            assert inner(fragment["text"]) == scenario.sources[evidence["locator"]]
            checked += 1
    assert checked > 10


def test_the_registry_and_the_run_locators_are_both_used(corpus):
    _, scenario = corpus
    kinds = {locator.split(":", 1)[0] for locator in scenario.sources}
    assert kinds == {"registry", "run"}


def test_the_traps_answer_with_their_reasons(fresh):
    env, scenario = fresh
    (stale,) = scenario.traps["stale_source"]
    code, result = run(env, "--principal", kc.PRINCIPAL_ALL, "--id", stale)
    assert code == 1 and result["status"] == "incomplete" and result["fragments"] == []
    assert [m["reason"] for m in result["unfetched"]] == ["hash_mismatch"]
    assert "60 秒" not in json.dumps(result, ensure_ascii=False)
    (missing,) = scenario.traps["missing_source"]
    code, result = run(env, "--principal", kc.PRINCIPAL_ALL, "--id", missing)
    assert code == 1 and [m["reason"] for m in result["unfetched"]] == ["source_unavailable"]


def test_the_injected_instruction_stays_inside_and_changes_nothing(fresh):
    env, scenario = fresh
    (injected,) = scenario.traps["instruction_text"]
    code, result = run(env, "--principal", kc.PRINCIPAL_ALL, "--id", injected)
    assert code == 0
    (fragment,) = result["fragments"]
    assert fragment["instruction_like"] is True and kc.INJECTION in inner(fragment["text"])
    assert result["record"]["enforcement"] == "advisory"
    assert result["record"]["status"] == "approved"
    code, again = run(env, "--principal", kc.PRINCIPAL_ALL, "--id", injected)
    assert again == result


# What must not leak ----------------------------------------------------------------------------


def test_an_unreadable_record_answers_exactly_like_a_missing_one(fresh, capsys):
    env, scenario = fresh
    third = sorted(
        record_id
        for record_id, value in by_id(scenario).items()
        if value["scope"]["project_id"] == kc.THIRD_PROJECT
        and value["status"] == "approved"
        and "access" in value
        and "team-gamma" in value["access"]["readers"]
    )[0]
    capsys.readouterr()
    hidden = raw(env, "fetch", "--principal", kc.PRINCIPAL_LIMITED, "--id", third)
    hidden_err = capsys.readouterr().err
    missing = raw(env, "fetch", "--principal", kc.PRINCIPAL_LIMITED, "--id", "KN-000000000000")
    missing_err = capsys.readouterr().err
    assert hidden[0] == missing[0] == 1
    assert hidden[1].replace(third, "KN-000000000000") == missing[1]
    assert hidden_err == missing_err
    code, wide = run(env, "--principal", kc.PRINCIPAL_ALL, "--id", third)
    assert code == 0 and wide["fragments"]


@pytest.mark.parametrize(
    "bad",
    [
        "../KN-3f2a9c01b7de",
        "KN-3f2a9c01b7de/../../x",
        "KN-3f2a9c01b7d\0e",
        "KN-" + "a" * 4096,
        "KN-3F2A9C01B7DE",
        "kn-3f2a9c01b7de",
        "KN-3f2a9c01b7d",
        "",
    ],
)
def test_ids_that_are_not_record_ids_exit_2_with_nothing_on_stdout(corpus, bad, capsys):
    env, _ = corpus
    code, text = raw(env, "fetch", "--principal", kc.PRINCIPAL_ALL, "--id", bad)
    assert code == 2 and text == ""
    assert "\0" not in capsys.readouterr().err


def test_a_secret_shaped_source_is_named_but_never_printed(home, tmp_path, capsys):
    write_config(home)
    fake = "AKIA" + "Q" * 16
    proof = registry_evidence(home, "RG-0001", f"# R1\n\naccess key {fake}\n")
    update(home, write_input(tmp_path / "in.json", [item("R1", evidence=[proof])]))
    capsys.readouterr()
    code, text = raw(
        home, "fetch", "--principal", "workspace-a", "--id", model.record_id("synthetic", "R1")
    )
    captured = capsys.readouterr()
    result = json.loads(text)
    assert code == 1 and result["withheld"][0]["shapes"] == ["aws_access_key"]
    assert fake not in text and fake not in captured.err


def test_a_deny_terms_file_withholds_the_source(home, tmp_path):
    write_config(home)
    proof = registry_evidence(home, "RG-0001", "# R1\n\nExampleCorp 向けの設定\n")
    update(home, write_input(tmp_path / "in.json", [item("R1", evidence=[proof])]))
    terms = tmp_path / "deny.txt"
    terms.write_text("examplecorp\n")
    argv = ["--principal", "workspace-a", "--id", model.record_id("synthetic", "R1")]
    code, result = run(home, *argv, "--deny-terms-file", str(terms))
    assert code == 1 and result["withheld"][0]["shapes"] == ["deny_term"]
    assert "examplecorp" not in json.dumps(result).lower()
    code, result = run(home, *argv)
    assert code == 0 and result["fragments"]


def test_git_evidence_is_read_from_the_bare_repository(home, tmp_path):
    write_config(home)
    text = "a = 1\nb = 2\nc = 3\n"
    sha = bare_repository(home, tmp_path / "w", "example-org/alpha", {"infra/q.tf": text})
    proof = git_evidence("infra/q.tf", sha, text, 2)
    value = item("G1", evidence=[proof], facets={"repo": "example-org/alpha"})
    update(home, write_input(tmp_path / "in.json", [value]))
    code, result = run(
        home, "--principal", "workspace-a", "--id", model.record_id("synthetic", "G1")
    )
    assert code == 0 and inner(result["fragments"][0]["text"]) == "b = 2"


def test_paging_through_the_cli_rebuilds_the_source(home, tmp_path):
    write_config(home)
    text = "".join(f"行 {n} のテキスト\n" for n in range(200))
    proof = registry_evidence(home, "RG-0001", text)
    update(home, write_input(tmp_path / "in.json", [item("R1", evidence=[proof])]))
    argv = ["--principal", "workspace-a", "--id", model.record_id("synthetic", "R1")]
    offset, pieces = 0, []
    while offset is not None:
        code, result = run(home, *argv, "--max-tokens", "250", "--offset", str(offset))
        assert code == 0 and result["budget"]["used_tokens"] <= 250
        pieces += [inner(f["text"]) for f in result["fragments"]]
        offset = result["next_offset"]
    assert "".join(pieces) == text and len(pieces) > 5


def test_an_old_generation_is_reported(fresh):
    env, scenario = fresh
    (injected,) = scenario.traps["instruction_text"]
    argv = ["--principal", kc.PRINCIPAL_ALL, "--id", injected]
    _, now = run(env, *argv)
    generation = now["snapshot"]["generation"]
    _, same = run(env, *argv, "--generation", str(generation))
    assert same["snapshot"]["moved"] is False
    _, old = run(env, *argv, "--generation", str(generation - 1))
    assert old["snapshot"] == {**now["snapshot"], "asked_generation": generation - 1, "moved": True}


@pytest.mark.parametrize(
    "argv",
    [
        ["--max-tokens", "0"],
        ["--offset", "-1"],
        ["--generation", "x"],
        ["--extend-task-budget", "100", "--reason", "x"],  # no --task
        ["--task", "T-1", "--extend-task-budget", "100"],  # no reason
        ["--task", "T-1", "--reason", "x"],  # a reason without an extension
        ["--task", "T-1", "--extend-task-budget", "0", "--reason", "x"],
        ["--task", "T-1", "--extend-task-budget", "10", "--reason", ""],
        ["--task", "T-1", "--extend-task-budget", "10", "--reason", "a\nb"],
        ["--task", "../T-1"],
        ["--task", "T" * 65],
    ],
)
def test_bad_arguments_exit_2(fresh, argv):
    env, scenario = fresh
    (injected,) = scenario.traps["instruction_text"]
    code, text = raw(env, "fetch", "--principal", kc.PRINCIPAL_ALL, "--id", injected, *argv)
    assert code == 2 and text == ""
    assert not (paths.state_root(env) / "knowledge" / "tasks").exists()


def test_a_secret_shaped_reason_is_refused_and_not_echoed(fresh, capsys):
    env, scenario = fresh
    (injected,) = scenario.traps["instruction_text"]
    fake = "ghp_" + "B" * 36
    argv = ["--task", "T-1", "--extend-task-budget", "10", "--reason", f"token {fake}"]
    code, text = raw(env, "fetch", "--principal", kc.PRINCIPAL_ALL, "--id", injected, *argv)
    assert code == 2 and text == "" and fake not in capsys.readouterr().err


def test_an_unknown_principal_exits_3(corpus):
    env, _ = corpus
    code, text = raw(env, "fetch", "--principal", "nobody", "--id", "KN-000000000000")
    assert code == 3 and text == ""


# The task total ---------------------------------------------------------------------------------


def test_search_without_a_task_counts_nothing(fresh):
    env, scenario = fresh
    code, found = search(env, *task_search(scenario.tasks[1]))
    assert code == 0 and found["budget"]["task"] is None
    assert not (paths.state_root(env) / "knowledge" / "tasks").exists()


def test_search_and_fetch_add_to_one_total_and_stop_at_the_limit(fresh):
    env, scenario = fresh
    task = scenario.tasks[1]
    (injected,) = scenario.traps["instruction_text"]
    code, found = search(env, *task_search(task, "--task", "T-1"))
    assert code == 0
    used = found["budget"]["used_tokens"]
    assert found["budget"]["task"] == {
        "id": "T-1",
        "limit": budget.DEFAULT_LIMIT,
        "used_before": 0,
        "used_after": used,
    }
    fetch_argv = ["--principal", task["principal"], "--id", injected, "--task", "T-1"]
    total, last = used, None
    while True:
        code, last = run(env, *fetch_argv)
        if last["status"] != "ok":
            break
        assert last["budget"]["task"]["used_before"] == total
        total = last["budget"]["task"]["used_after"]
        assert total == last["budget"]["task"]["used_before"] + last["budget"]["used_tokens"]
    assert code == 1 and last["status"] == "task_budget_exhausted"
    assert last["fragments"] == [] and last["budget"]["used_tokens"] == 0
    stopped = last["budget"]["task"]
    assert stopped["used_before"] == stopped["used_after"] == total
    assert total + stopped["needed_tokens"] > budget.DEFAULT_LIMIT >= total
    code, again = search(env, *task_search(task, "--task", "T-1"))
    if used > budget.DEFAULT_LIMIT - total:
        assert code == 1 and again["status"] == "task_budget_exhausted"
        assert again["records"] == [] and again["budget"]["task"]["needed_tokens"] == used
    extended = [*fetch_argv, "--extend-task-budget", "1000", "--reason", "原文の続きが要る"]
    code, result = run(env, *extended)
    assert code == 0 and result["status"] == "ok" and result["fragments"]
    assert result["budget"]["task"]["limit"] == budget.DEFAULT_LIMIT + 1000
    entries = budget.entries(paths.state_root(env), "T-1", task["principal"])
    (extension,) = [row for row in entries if row["event"] == "extend"]
    assert extension["by"] == 1000 and extension["reason"] == "原文の続きが要る"
    assert extension["principal"] == task["principal"] and extension["at"] == NOW
    assert {row["command"] for row in entries if row["event"] == "call"} == {"search", "fetch"}
    ledger = budget.ledger(paths.state_root(env), "T-1", task["principal"]).read_text()
    assert kc.INJECTION not in ledger and "保持期間" not in ledger and "UNTRUSTED" not in ledger


def test_the_same_search_differs_only_by_the_task_total(fresh):
    env, scenario = fresh
    task = scenario.tasks[2]
    _, first = search(env, *task_search(task, "--task", "T-2"))
    _, second = search(env, *task_search(task, "--task", "T-2"))
    assert second["budget"]["task"]["used_before"] == first["budget"]["task"]["used_after"]
    first["budget"]["task"] = second["budget"]["task"] = None
    assert first == second


# The synthetic source texts ---------------------------------------------------------------------


def test_the_corpus_script_writes_the_source_texts_into_the_state_root(tmp_path):
    env = {**os.environ, "AI_HARNESS_HOME": str(tmp_path / "state")}
    done = subprocess.run(
        [
            sys.executable,
            "-m",
            "examples.synthetic.knowledge_corpus",
            str(tmp_path / "out"),
            "--size",
            "50",
            "--write-sources",
        ],
        cwd=RUNTIME,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr
    written = json.loads(done.stdout)
    root = state_root(env)
    assert written["source_texts"]["registry"] > 0 and written["source_texts"]["runs"] == [
        kc.RUN_ID
    ]
    assert any((root / "registry").glob("RG-*/v0001.md"))
    rows = records.read_jsonl(root / "runs" / kc.RUN_ID / "documents.jsonl")
    assert rows and all(records.violations("spec-document", row) == [] for row in rows)
