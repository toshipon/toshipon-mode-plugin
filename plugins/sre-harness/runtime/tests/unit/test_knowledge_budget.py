"""The task total: one append-only ledger per (task, principal) under knowledge/tasks/."""

import json
import stat
import subprocess
import sys
import threading

import pytest

from sre_harness import paths
from sre_harness.errors import InputError
from sre_harness.knowledge import budget
from tests.invariants.test_boundaries import RUNTIME

NOW = "2026-11-27T00:00:00Z"


@pytest.fixture
def root(home):
    return paths.state_root(home)


def charge(root, tokens, task="TASK-0001", principal="workspace-a", **options):
    return budget.charge(
        root,
        budget.Task(task, options.pop("extend_by", None), options.pop("reason", None)),
        principal,
        command=options.pop("command", "fetch"),
        ids=options.pop("ids", ["KN-000000000001"]),
        tokens=tokens,
        generation=options.pop("generation", 3),
        now=options.pop("now", NOW),
        **options,
    )


def rows(root, task="TASK-0001", principal="workspace-a"):
    return budget.entries(root, task, principal)


def test_the_ledger_is_named_by_a_hash_and_is_private(root):
    charge(root, 10, task="../../etc/passwd")
    path = budget.ledger(root, "../../etc/passwd", "workspace-a")
    assert path.parent == root / "knowledge" / "tasks"
    assert path.name.endswith(".jsonl") and "passwd" not in path.name and ".." not in path.name
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert budget.ledger(root, "../../etc/passwd", "workspace-b") != path
    assert budget.ledger(root, "TASK-0002", "workspace-a") != path


def test_calls_add_up_until_the_limit(root):
    first = charge(root, 4000)
    assert first.allowed and first.view == {
        "id": "TASK-0001",
        "limit": 6000,
        "used_before": 0,
        "used_after": 4000,
    }
    second = charge(root, 2000)
    assert second.allowed and second.view["used_before"] == 4000
    assert second.view["used_after"] == 6000


def test_a_call_that_would_go_over_is_refused_and_not_counted(root):
    charge(root, 5000)
    refused = charge(root, 1001)
    assert not refused.allowed
    assert refused.view == {
        "id": "TASK-0001",
        "limit": 6000,
        "used_before": 5000,
        "used_after": 5000,
        "needed_tokens": 1001,
    }
    assert charge(root, 1000).allowed  # what still fits goes through
    assert charge(root, 0).allowed  # nothing to return costs nothing, even at the limit
    assert not charge(root, 1).allowed


def test_the_limit_can_be_given_and_tasks_and_principals_are_apart(root):
    assert not charge(root, 11, limit=10).allowed
    assert charge(root, 6000, task="TASK-0002").allowed
    assert charge(root, 6000, principal="workspace-b").allowed


def test_an_extension_is_recorded_and_raises_the_limit(root):
    charge(root, 6000)
    assert not charge(root, 500).allowed
    extended = charge(
        root, 500, extend_by=1000, reason="追加の根拠が要る", now="2026-11-27T01:00:00Z"
    )
    assert extended.allowed and extended.view["limit"] == 7000
    assert extended.view["used_before"] == 6000 and extended.view["used_after"] == 6500
    (extension,) = [row for row in rows(root) if row["event"] == "extend"]
    assert extension == {
        "event": "extend",
        "task": "TASK-0001",
        "principal": "workspace-a",
        "by": 1000,
        "reason": "追加の根拠が要る",
        "at": "2026-11-27T01:00:00Z",
    }
    assert charge(root, 400).view["limit"] == 7000  # the extension stays


def test_the_ledger_holds_counts_and_ids_but_no_text(root):
    charge(root, 120, command="search", ids=["KN-000000000001", "KN-000000000002"])
    charge(root, 99999, command="fetch")
    calls = [row for row in rows(root) if row["event"] == "call"]
    assert calls == [
        {
            "event": "call",
            "task": "TASK-0001",
            "principal": "workspace-a",
            "command": "search",
            "ids": ["KN-000000000001", "KN-000000000002"],
            "tokens": 120,
            "needed": 120,
            "charged": True,
            "generation": 3,
            "at": NOW,
        },
        {
            "event": "call",
            "task": "TASK-0001",
            "principal": "workspace-a",
            "command": "fetch",
            "ids": ["KN-000000000001"],
            "tokens": 0,
            "needed": 99999,
            "charged": False,
            "generation": 3,
            "at": NOW,
        },
    ]


def test_a_damaged_ledger_stops_instead_of_guessing(root):
    charge(root, 10)
    path = budget.ledger(root, "TASK-0001", "workspace-a")
    with open(path, "a") as stream:
        stream.write('{"event": "call", "tokens": \n')
    with pytest.raises(InputError):
        charge(root, 10)
    path.write_text(json.dumps({"event": "other"}) + "\n")
    with pytest.raises(InputError):
        rows(root)


def test_no_ledger_yet_means_nothing_used(root):
    assert rows(root) == []


def test_two_threads_appending_at_once_lose_no_line(root):
    def work():
        for _ in range(50):
            charge(root, 1, limit=100000)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    calls = rows(root)
    assert len(calls) == 100 and sum(row["tokens"] for row in calls) == 100


WORKER = """
import sys
from sre_harness import paths
from sre_harness.knowledge import budget
root = paths.state_root({"AI_HARNESS_HOME": sys.argv[1]})
for _ in range(40):
    budget.charge(root, budget.Task("TASK-0001"), "workspace-a", command="fetch",
                  ids=["KN-000000000001"], tokens=7, generation=1,
                  now="2026-11-27T00:00:00Z", limit=6000)
"""


def test_two_processes_appending_at_once_lose_no_line_and_keep_the_limit(home, root):
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", WORKER, home["AI_HARNESS_HOME"]],
            cwd=RUNTIME,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for worker in workers:
        _, err = worker.communicate(timeout=120)
        assert worker.returncode == 0, err
    calls = rows(root)
    assert len(calls) == 80
    assert sum(row["tokens"] for row in calls) == 80 * 7
    assert all(row["charged"] for row in calls)


def test_the_processes_together_cannot_pass_the_limit(home, root):
    script = WORKER.replace("tokens=7", "tokens=100")
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", script, home["AI_HARNESS_HOME"]],
            cwd=RUNTIME,
            stderr=subprocess.PIPE,
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.communicate(timeout=120)
    calls = rows(root)
    assert len(calls) == 80
    assert sum(row["tokens"] for row in calls) == 6000
    assert sum(row["charged"] for row in calls) == 60
