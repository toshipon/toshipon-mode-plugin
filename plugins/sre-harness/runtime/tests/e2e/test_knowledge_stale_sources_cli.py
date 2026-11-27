"""`stale_index` is decided per source: a config change stops every reader until every source
has been taken in again under the new config, not only the one that was taken in first."""

import copy
import json

import pytest

from sre_harness import records
from sre_harness.knowledge import model
from tests.helpers import run_cli
from tests.knowledge_helpers import CONFIG, item, update, write_config, write_input

ALPHA = "file:synthetic"
OTHER = "file:other"


def task_file(tmp_path, principal="workspace-a"):
    value = {
        "schema_version": 1,
        "task_id": "TASK-0001",
        "workspace_id": "ws-a",
        "principal": principal,
        "project_id": "project-alpha",
        "component_id": None,
        "repo": "example-org/alpha",
        "base_sha": None,
        "changed_paths": [],
        "resource_types": [],
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def two_sources(home, tmp_path):
    """Two file sources taken in under CONFIG, then a config change and only `other` taken in
    again: `synthetic` still holds records built under the old config."""
    write_config(home)
    alpha = write_input(tmp_path / "alpha.json", [item("R1")])
    other = write_input(tmp_path / "other.json", [item("O1")], source="other")
    assert update(home, alpha)[0] == 0 and update(home, other)[0] == 0
    changed = copy.deepcopy(CONFIG)
    changed["principals"]["workspace-c"] = {
        "readers": ["team-alpha"],
        "classifications": ["internal"],
    }
    write_config(home, changed)
    code, result = update(home, other)
    assert code == 0 and result["config_changed"] is True
    return home, alpha


def test_taking_in_one_source_does_not_lift_stale_index_for_the_others(two_sources):
    env, alpha = two_sources
    argv = ["knowledge", "search", "--principal", "workspace-a", "--project", "project-alpha"]
    code, result = run_cli(argv, env)
    assert code == 1 and result["status"] == "stale_index" and result["records"] == []
    assert result["stale_sources"] == [ALPHA]
    assert records.violations("knowledge-search-result", result) == []
    assert update(env, alpha)[0] == 0
    code, result = run_cli(argv, env)
    assert code == 0 and result["status"] == "ok" and "stale_sources" not in result


def test_fetch_context_and_check_stop_while_one_source_is_stale(two_sources, tmp_path):
    env, _ = two_sources
    record_id = model.record_id("synthetic", "R1")
    code, fetched = run_cli(
        ["knowledge", "fetch", "--principal", "workspace-a", "--id", record_id], env
    )
    assert code == 1 and fetched["status"] == "stale_index" and fetched["stale_sources"] == [ALPHA]
    assert records.violations("knowledge-fetch-result", fetched) == []
    task = task_file(tmp_path)
    code, found = run_cli(["knowledge", "context", "--task-file", str(task)], env)
    assert code == 1 and found["status"] == "stale_index" and found["stale_sources"] == [ALPHA]
    patch = tmp_path / "change.diff"
    patch.write_text(
        "diff --git a/a.txt b/a.txt\n--- a/a.txt\n+++ b/a.txt\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    )
    code, checked = run_cli(
        ["knowledge", "check", "--task-file", str(task), "--patch", str(patch)], env
    )
    assert code == 1 and checked["status"] == "stale_index" and checked["stale_sources"] == [ALPHA]
    assert records.violations("knowledge-check-result", checked) == []


def test_export_and_decision_put_stop_while_one_source_is_stale(two_sources, tmp_path, capsys):
    env, _ = two_sources
    code, out = run_cli(
        ["knowledge", "export", "--principal", "workspace-a", "--format", "context-entities"], env
    )
    assert code == 1 and out is None
    assert ALPHA in capsys.readouterr().err
    code, out = run_cli(
        [
            "knowledge",
            "decision",
            "put",
            "--task-file",
            str(task_file(tmp_path)),
            "--question",
            "q?",
            "--option",
            "yes",
            "--option",
            "no",
            "--evidence",
            model.record_id("synthetic", "R1"),
            "--risk",
            "low",
        ],
        env,
    )
    assert code == 1 and out is None
    assert ALPHA in capsys.readouterr().err


def test_status_says_which_sources_follow_the_config(two_sources):
    env, alpha = two_sources
    code, status = run_cli(["knowledge", "status"], env)
    assert code == 0
    assert {row["name"]: row["config_current"] for row in status["sources"]} == {
        ALPHA: False,
        OTHER: True,
    }
    assert all("config_hash" not in row for row in status["sources"])
    update(env, alpha)
    code, status = run_cli(["knowledge", "status"], env)
    assert {row["config_current"] for row in status["sources"]} == {True}
