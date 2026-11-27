"""`sre-harness knowledge migrate / rollback` and `update --source review-registry` end to end,
through `cli.main`, on a synthetic registry. Also: the review commands that read the registry
are not changed by any of it, and a reader in another process is not disturbed by the swap."""

import io
import json
import os
import subprocess
import sys

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import cli, paths, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import model, store
from sre_harness.review import approve, registry
from tests.invariants.test_boundaries import RUNTIME
from tests.knowledge_helpers import item, update, write_input

NOW = "2026-11-27T06:00:00Z"
ORIGIN = "review-registry"
ALPHA = ["--principal", "workspace-a", "--project", "project-alpha"]


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(knowledge_cli, "clock", lambda: NOW)


@pytest.fixture
def env(tmp_path):
    value = {"AI_HARNESS_HOME": str(tmp_path / "state")}
    root = paths.state_root(value)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    rr.write(root)
    return value


@pytest.fixture
def indexed(env, tmp_path):
    """The index before the migration: one record of a file source."""
    code, _ = update(env, write_input(tmp_path / "in.json", [item("R1")]))
    assert code == 0
    return env


def knowledge(env, stage, *argv, schema=None):
    out = io.StringIO()
    code = cli.main(["knowledge", stage, *argv], env=env, out=out)
    text = out.getvalue()
    value = json.loads(text) if text.strip() else None
    if schema and value is not None:
        assert records.violations(schema, value) == [], value
    return code, value


def search(env, *argv):
    return knowledge(env, "search", *argv, schema="knowledge-search-result")


def fetch(env, *argv):
    return knowledge(env, "fetch", *argv, schema="knowledge-fetch-result")


def root_of(env):
    return paths.state_root(env)


def key(entry):
    return model.record_id(ORIGIN, entry)


def tree(path):
    return {
        str(child.relative_to(path)): child.read_bytes()
        for child in sorted(path.rglob("*"))
        if child.is_file()
    }


def registry_files(env):
    base = root_of(env) / "registry"
    return {
        str(path.relative_to(base)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def active(env):
    root = root_of(env)
    return {repo: registry.active_rules(root, repo) for repo in (rr.ALPHA, rr.GAMMA, rr.STRAY)}


def without_snapshot(result):
    return {name: value for name, value in result.items() if name != "snapshot"}


# migrate -----------------------------------------------------------------------------------


def test_a_dry_run_through_the_command_line_writes_nothing(indexed):
    before = tree(root_of(indexed) / "knowledge")
    code, result = knowledge(indexed, "migrate", "--dry-run")
    assert code == 0 and result["dry_run"] is True and result["replaced"] is False
    assert tree(root_of(indexed) / "knowledge") == before
    assert {row["enforcement"] for row in result["plan"]["records"]} == {"advisory"}
    assert [row["entry"] for row in result["plan"]["unmapped"]] == ["RG-9006"]
    assert [row["entry"] for row in result["plan"]["invalid"]] == ["RG-9007"]


def test_migrated_records_are_advisory_searchable_and_fetchable(indexed):
    code, result = knowledge(indexed, "migrate")
    assert code == 0 and result["check"]["ok"] is True and result["replaced"] is True
    code, found = search(indexed, *ALPHA)
    assert code == 0
    shown = {row["id"]: row for row in found["records"]}
    assert {key("RG-9001"), key("RG-9002")} <= set(shown)
    assert found["mandatory"]["count"] == 0
    assert {row["enforcement"] for row in found["records"]} == {"advisory"}
    # retired, rejected, unmapped and invalid entries are not returned
    assert not {key(e) for e in ("RG-9003", "RG-9004", "RG-9006", "RG-9007")} & set(shown)
    code, fetched = fetch(indexed, "--principal", "workspace-a", "--id", key("RG-9001"))
    assert code == 0 and fetched["status"] == "ok"
    (fragment,) = fetched["fragments"]
    text = (root_of(indexed) / "registry" / "RG-9001" / "v0001.md").read_text(encoding="utf-8")
    assert fragment["locator"] == "registry:RG-9001/v0001.md" and fragment["hash_match"]
    assert fragment["content_sha256"] == records.sha256(text)
    assert "timeout" in fragment["text"]


def test_a_policy_check_is_found_by_its_path(indexed):
    knowledge(indexed, "migrate")
    code, found = search(indexed, *ALPHA, "--path", "infra/queue/main.tf")
    ids = [row["id"] for row in found["records"]]
    assert code == 0 and key("RG-9002") in ids
    (queue,) = [row for row in found["records"] if row["id"] == key("RG-9002")]
    assert "path_match" in queue["why_selected"]
    code, found = search(indexed, *ALPHA, "--path", "docs/readme.md")
    assert key("RG-9002") not in [row["id"] for row in found["records"]]


def test_another_project_sees_only_its_own_entries(indexed):
    knowledge(indexed, "migrate")
    code, found = search(indexed, "--principal", "workspace-a", "--project", "project-gamma")
    assert code == 0 and [row["id"] for row in found["records"]] == [key("RG-9005")]
    code, found = search(indexed, "--principal", "workspace-b", "--project", "project-gamma")
    assert code == 0 and found["records"] == []
    code, fetched = fetch(indexed, "--principal", "workspace-b", "--id", key("RG-9005"))
    assert code == 1 and fetched["unfetched"] == [{"reason": "access_denied"}]


def test_retired_and_rejected_entries_are_withdrawn(indexed):
    knowledge(indexed, "migrate")
    for entry in ("RG-9003", "RG-9004"):
        code, fetched = fetch(indexed, "--principal", "workspace-a", "--id", key(entry))
        assert code == 1 and fetched["status"] == "withdrawn" and fetched["fragments"] == []


def test_a_mismatch_exits_1_and_leaves_the_index_as_it_was(indexed):
    root = root_of(indexed)
    code, before = search(indexed, *ALPHA)
    rr.write_entry(root, "RG-9300", rr.ALPHA, [("approved", rr.text("後で書き換える", "本文。"))])
    rr.tamper(root, "RG-9300", 1)
    files = tree(root / "knowledge")
    code, result = knowledge(indexed, "migrate")
    assert code == 1 and result["replaced"] is False and result["check"]["ok"] is False
    assert tree(root / "knowledge") == files
    code, after = search(indexed, *ALPHA)
    assert code == 0 and after == before


# update --source review-registry -------------------------------------------------------------


def test_a_new_version_is_taken_in_and_an_unchanged_registry_does_not_advance(indexed):
    root = root_of(indexed)
    code, first = knowledge(indexed, "update", "--source", "review-registry")
    assert code == 0 and first["generation"] == {"before": 1, "after": 2}
    code, again = knowledge(indexed, "update", "--source", "review-registry")
    assert again["generation"] == {"before": 2, "after": 2} and again["advanced"] is False
    approve.retire(root, "RG-9001", "alice", "合成データの取り下げ")
    code, moved = knowledge(indexed, "update", "--source", "review-registry")
    assert moved["generation"] == {"before": 2, "after": 3}
    assert moved["counts"]["updated"] == 1
    code, found = search(indexed, *ALPHA)
    assert key("RG-9001") not in [row["id"] for row in found["records"]]
    code, fetched = fetch(indexed, "--principal", "workspace-a", "--id", key("RG-9001"))
    assert fetched["status"] == "withdrawn"


def test_a_newly_approved_version_replaces_the_text_returned(indexed):
    root = root_of(indexed)
    knowledge(indexed, "migrate")
    revised = rr.text("外向きの HTTP 呼び出しには 10 秒の timeout を設定する", "改訂。")
    record = rr.version("RG-9001", 2, "approved", rr.ALPHA, revised, previous=1)
    registry.write_version(root, record, revised)
    code, moved = knowledge(indexed, "update", "--source", "review-registry")
    assert code == 0 and moved["counts"]["updated"] == 1
    code, found = search(indexed, *ALPHA)
    (shown,) = [row for row in found["records"] if row["id"] == key("RG-9001")]
    assert shown["summary"].startswith("外向きの HTTP 呼び出しには 10 秒")
    assert shown["evidence"][0]["locator"] == "registry:RG-9001/v0002.md"
    code, fetched = fetch(indexed, "--principal", "workspace-a", "--id", key("RG-9001"))
    assert code == 0 and "改訂。" in fetched["fragments"][0]["text"]


def test_the_review_registry_source_refuses_an_input_file(indexed, tmp_path):
    argv = ["--source", "review-registry", "--input", str(tmp_path / "x.json")]
    code, _ = knowledge(indexed, "update", *argv)
    assert code == 2


# rollback ----------------------------------------------------------------------------------


def test_a_rollback_gives_back_the_search_of_before_the_migration(indexed):
    code, before = search(indexed, *ALPHA)
    assert code == 0 and before["snapshot"]["generation"] == 1
    code, migrated = knowledge(indexed, "migrate")
    code, during = search(indexed, *ALPHA)
    assert during["snapshot"]["generation"] == 2 and len(during["records"]) > 1
    code, back = knowledge(indexed, "rollback", "--to", migrated["backup"]["name"])
    assert code == 0 and back["generation"] == {"before": 2, "after": 3}
    code, after = search(indexed, *ALPHA)
    assert code == 0 and after["snapshot"]["generation"] == 3
    assert without_snapshot(after) == without_snapshot(before)


def test_rollback_lists_the_backups(indexed):
    code, migrated = knowledge(indexed, "migrate")
    code, listed = knowledge(indexed, "rollback", "--list")
    assert code == 0 and [row["name"] for row in listed["backups"]] == [migrated["backup"]["name"]]


@pytest.mark.parametrize("name", ["../index.sqlite3", "x/../../index.sqlite3", "index.sqlite3"])
def test_rollback_refuses_a_name_it_did_not_make(indexed, name):
    files = tree(root_of(indexed) / "knowledge")
    code, _ = knowledge(indexed, "rollback", "--to", name)
    assert code == 2 and tree(root_of(indexed) / "knowledge") == files


def test_rollback_to_a_missing_backup_exits_2(indexed):
    code, _ = knowledge(indexed, "rollback", "--to", "index-20261127T000000Z-g0001.sqlite3")
    assert code == 2


def test_a_held_lock_exits_1_for_migrate_and_rollback(indexed):
    code, migrated = knowledge(indexed, "migrate")
    with store.writer(root_of(indexed)):
        files = tree(root_of(indexed) / "knowledge")
        assert knowledge(indexed, "migrate")[0] == 1
        assert knowledge(indexed, "rollback", "--to", migrated["backup"]["name"])[0] == 1
        assert tree(root_of(indexed) / "knowledge") == files


# The review commands do not change --------------------------------------------------------------


def test_migrate_and_rollback_leave_the_registry_and_its_readers_alone(indexed):
    files, rules = registry_files(indexed), active(indexed)
    code, migrated = knowledge(indexed, "migrate")
    assert code == 0
    assert knowledge(indexed, "migrate", "--dry-run")[0] == 0
    assert knowledge(indexed, "update", "--source", "review-registry")[0] == 0
    assert knowledge(indexed, "rollback", "--to", migrated["backup"]["name"])[0] == 0
    assert registry_files(indexed) == files
    assert active(indexed) == rules


# A reader in another process ----------------------------------------------------------------

READER = """
import json, sys
from pathlib import Path
from sre_harness.knowledge import config, store

root = Path(sys.argv[1])
principal = config.principal(config.load(root), "workspace-a")
with store.reader(root) as reader:
    for _ in range(2):
        rows = reader.records(principal)
        print(json.dumps({"generation": reader.meta()["generation"],
                          "ids": [row["id"] for row in rows]}), flush=True)
        sys.stdin.readline()
"""


def reader_process(env):
    child = {**os.environ, "AI_HARNESS_HOME": env["AI_HARNESS_HOME"], "PYTHONPATH": str(RUNTIME)}
    process = subprocess.Popen(
        [sys.executable, "-c", READER, str(root_of(env))],
        cwd=RUNTIME,
        env=child,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert process.stdout is not None
    return process, json.loads(process.stdout.readline())


def finish(process):
    stdout, _ = process.communicate("\n\n", timeout=60)
    assert process.returncode == 0
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_a_reader_open_during_the_swap_keeps_reading_its_own_snapshot(indexed):
    process, first = reader_process(indexed)
    try:
        code, migrated = knowledge(indexed, "migrate")
        assert code == 0 and migrated["replaced"] is True
        process.stdin.write("\n")
        process.stdin.flush()
        second = json.loads(process.stdout.readline())
    finally:
        rest = finish(process)
    assert rest == []
    assert first == second and first["generation"] == 1
    assert first["ids"] == [model.record_id("synthetic", "R1")]
    code, found = search(indexed, *ALPHA)
    assert found["snapshot"]["generation"] == 2 and len(found["records"]) == 3
    # the index after the swap takes the next writer, and readers see it whole
    assert knowledge(indexed, "update", "--source", "review-registry")[0] == 0
    code, status = knowledge(indexed, "status")
    assert status["generation"] == 2 and status["records"]["approved"] == 4


def test_a_reader_holding_frames_of_the_old_wal_makes_migrate_wait_then_exit_1(
    indexed, tmp_path, monkeypatch
):
    monkeypatch.setattr(store, "TIMEOUT_SECONDS", 0.2)
    holder, _ = reader_process(indexed)  # keeps the writer below from cleaning up its WAL
    try:
        assert update(indexed, write_input(tmp_path / "two.json", [item("R1"), item("R2")]))[0] == 0
    finally:
        finish(holder)
    folder = root_of(indexed) / "knowledge"
    assert (folder / (store.INDEX + "-wal")).stat().st_size > 0
    process, first = reader_process(indexed)  # reads generation 2 from the WAL
    try:
        assert first["generation"] == 2
        inode = (folder / store.INDEX).stat().st_ino
        code, before = search(indexed, *ALPHA)
        code, _ = knowledge(indexed, "migrate")
        assert code == 1
        # Not replaced and not backed up. (The checkpoint that found the reader may have
        # copied frames into the file, so its bytes are not compared; what it holds is.)
        assert (folder / store.INDEX).stat().st_ino == inode
        assert not (folder / "backups").exists()
        assert search(indexed, *ALPHA) == (0, before)
    finally:
        finish(process)
    code, migrated = knowledge(indexed, "migrate")
    assert code == 0 and migrated["generation"] == {"before": 2, "after": 3}
