"""`knowledge check`: the approved declarative checks that apply to a task, applied to a patch
with the interpreter `review evaluate` uses. Deterministic, no model. Everything is synthetic."""

import io
import json

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import cli, paths, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import model
from tests.knowledge_helpers import (
    item,
    project_block,
    registry_entry,
    state_root,
    update,
    write_input,
)

NOW = "2026-11-27T06:00:00Z"
SCHEMA = "knowledge-check-result"
ALPHA, GAMMA = rr.ALPHA, rr.GAMMA
EVAL_CHECK = {
    "path_globs": ["src/**/*.py"],
    "added_line_pattern": "\\beval\\(",
    "message": "eval を使わない",
}

QUEUE_BAD = """\
@@ -1,3 +1,4 @@
 resource "example_queue" "q" {
+  message_retention_seconds = 86400
   name = "q"
 }"""
QUEUE_GOOD = """\
@@ -1,3 +1,4 @@
 resource "example_queue" "q" {
+  message_retention_seconds = 1209600
   name = "q"
 }"""
CLIENT_BAD = """\
@@ -10,2 +10,4 @@
 import requests
+
+reply = requests.get(url)
 done = True"""
CLIENT_GOOD = """\
@@ -10,2 +10,3 @@
 import requests
+reply = requests.get(url, timeout=5)
 done = True"""


def diff(files):
    """A git-style unified diff of {path: hunks text}."""
    parts = []
    for path, hunks in files.items():
        parts += [f"diff --git a/{path} b/{path}", "index 1111111..2222222 100644"]
        parts += [f"--- a/{path}", f"+++ b/{path}", hunks]
    return "\n".join(parts) + "\n"


def rid(entry):
    return model.record_id("review-registry", entry)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    monkeypatch.setattr(knowledge_cli, "clock", lambda: NOW)


@pytest.fixture
def env(home):
    """A registry of five entries, taken into the index:

    RG-0001 alpha  queue retention check, approved, mandatory (knowledge block)
    RG-0002 alpha  HTTP timeout check, approved, advisory (no block)
    RG-0003 alpha  prose, approved
    RG-0004 gamma  eval check, approved (readable by workspace-a only)
    RG-0005 alpha  eval check, approved then retired
    """
    root = state_root(home)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    queue = rr.text("キューの保持期間は 14 日にする", "保持期間を明示する。", rr.QUEUE_CHECK)
    timeout = rr.text("HTTP の timeout を検査する", "宣言的検査。", rr.TIMEOUT_CHECK)
    prose = rr.text("ログには要求 ID を残す", "文章のルール。")
    evaluated = rr.text("eval を使わない", "宣言的検査。", EVAL_CHECK)
    mandatory = project_block("project-alpha", "mandatory", ["team-alpha"])
    registry_entry(root, "RG-0001", ALPHA, [("approved", queue)], knowledge=mandatory)
    registry_entry(root, "RG-0002", ALPHA, [("approved", timeout)])
    registry_entry(root, "RG-0003", ALPHA, [("approved", prose)])
    registry_entry(root, "RG-0004", GAMMA, [("approved", evaluated)])
    registry_entry(root, "RG-0005", ALPHA, [("approved", evaluated), ("retired", evaluated)])
    code, _, _ = knowledge(home, "update", "--source", "review-registry")
    assert code == 0
    return home


def knowledge(env, stage, *argv):
    out = io.StringIO()
    code = cli.main(["knowledge", stage, *argv], env=env, out=out)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None), text


def task(tmp_path, principal="workspace-b", project="project-alpha", repo=ALPHA, **changes):
    value = {
        "schema_version": 1,
        "task_id": "TASK-0001",
        "workspace_id": "ws-a",
        "principal": principal,
        "project_id": project,
        "component_id": None,
        "repo": repo,
        "base_sha": None,
        "changed_paths": [],
        "resource_types": [],
        **changes,
    }
    path = tmp_path / "task.json"
    path.write_text(json.dumps(value))
    return path


def check(env, tmp_path, patch, *, task_path=None, raw=False):
    patch_path = tmp_path / "change.diff"
    if isinstance(patch, bytes):
        patch_path.write_bytes(patch)
    else:
        patch_path.write_text(patch, encoding="utf-8")
    task_path = task_path or task(tmp_path)
    code, value, text = knowledge(
        env, "check", "--task-file", str(task_path), "--patch", str(patch_path)
    )
    if value is not None:
        assert records.violations(SCHEMA, value) == [], value
    return (code, value, text) if raw else (code, value)


BAD = diff({"infra/queue/main.tf": QUEUE_BAD, "src/app/client.py": CLIENT_BAD})
GOOD = diff({"infra/queue/main.tf": QUEUE_GOOD, "src/app/client.py": CLIENT_GOOD})


def test_violations_name_the_record_the_path_the_line_and_the_check(env, tmp_path):
    code, found = check(env, tmp_path, BAD)
    assert code == 1 and found["status"] == "violations"
    assert found["violations"] == [
        {
            "id": rid("RG-0001"),
            "enforcement": "mandatory",
            "path": "infra/queue/main.tf",
            "line": 2,
            "check": "added_line_pattern_and_absent_pattern",
        },
        {
            "id": rid("RG-0002"),
            "enforcement": "advisory",
            "path": "src/app/client.py",
            "line": 12,
            "check": "added_line_pattern_and_absent_pattern",
        },
    ]
    assert found["counts"] == {"mandatory": 1, "advisory": 1}
    assert found["patch"]["files"] == 2 and found["patch"]["hunks"] == 2


def test_a_patch_that_keeps_the_checks_passes(env, tmp_path):
    code, found = check(env, tmp_path, GOOD)
    assert (code, found["status"], found["violations"]) == (0, "ok", [])
    assert found["counts"] == {"mandatory": 0, "advisory": 0}
    applied = {row["id"]: row["enforcement"] for row in found["applied"]}
    assert applied == {rid("RG-0001"): "mandatory", rid("RG-0002"): "advisory"}


def test_only_the_mandatory_violation_is_marked_mandatory(env, tmp_path):
    _, found = check(env, tmp_path, diff({"infra/queue/main.tf": QUEUE_BAD}))
    assert found["counts"] == {"mandatory": 1, "advisory": 0}
    _, found = check(env, tmp_path, diff({"src/app/client.py": CLIENT_BAD}))
    assert found["counts"] == {"mandatory": 0, "advisory": 1}
    assert [v["enforcement"] for v in found["violations"]] == ["advisory"]


def test_prose_rules_are_listed_for_another_reader_and_not_judged(env, tmp_path):
    _, found = check(env, tmp_path, GOOD)
    assert found["not_checked"] == [{"id": rid("RG-0003"), "reason": "prose"}]
    assert rid("RG-0003") not in {row["id"] for row in found["applied"]}


def test_a_check_the_principal_cannot_read_is_not_applied_or_named(env, tmp_path):
    patch = diff({"src/app/run.py": "@@ -1,1 +1,2 @@\n x = 1\n+eval(x)"})
    code, found, text = check(env, tmp_path, patch, raw=True)
    assert code == 0 and found["violations"] == []
    assert rid("RG-0004") not in text
    gamma = task(tmp_path, principal="workspace-a", project="project-gamma", repo=GAMMA)
    code, found = check(env, tmp_path, patch, task_path=gamma)
    assert code == 1
    assert [(v["id"], v["line"]) for v in found["violations"]] == [(rid("RG-0004"), 2)]


def test_a_retired_check_is_not_applied(env, tmp_path):
    patch = diff({"src/app/run.py": "@@ -1,1 +1,2 @@\n x = 1\n+eval(x)"})
    _, found, text = check(env, tmp_path, patch, raw=True)
    assert rid("RG-0005") not in text


def test_a_check_whose_artifact_changed_is_not_applied(env, tmp_path):
    rr.tamper(state_root(env), "RG-0001", 1)
    code, found = check(env, tmp_path, BAD)
    assert code == 1
    assert {"id": rid("RG-0001"), "reason": "hash_mismatch"} in found["unfetched"]
    assert rid("RG-0001") not in {row["id"] for row in found["applied"]}
    assert rid("RG-0001") not in {row["id"] for row in found["violations"]}


def test_the_check_comes_from_the_artifact_not_from_the_recorded_copy(env, tmp_path):
    """A version record whose stored check disagrees with its artifact is refused: nothing
    from the record's own copy is ever applied."""
    root = state_root(env)
    path = paths.child(root, "registry", "RG-0002", "v0001.json")
    version = json.loads(path.read_text())
    version["candidate"]["check"]["added_line_pattern"] = "never-matches-anything"
    paths.write_private(path, records.canonical(version))
    code, found = check(env, tmp_path, BAD)
    assert code == 1
    assert {"id": rid("RG-0002"), "reason": "content_mismatch"} in found["unfetched"]
    assert rid("RG-0002") not in {row["id"] for row in found["violations"]}


def test_a_missing_version_record_is_unavailable(env, tmp_path):
    paths.child(state_root(env), "registry", "RG-0002", "v0001.json").unlink()
    code, found = check(env, tmp_path, GOOD)
    assert code == 1 and found["status"] == "incomplete"
    assert {"id": rid("RG-0002"), "reason": "source_unavailable"} in found["unfetched"]


def test_a_policy_check_without_a_registry_source_cannot_be_applied(env, tmp_path):
    loose = item("POLICY-1", kind="policy_check")
    code, _ = update(env, write_input(tmp_path / "in.json", [loose]))
    assert code == 0
    code, found = check(env, tmp_path, GOOD)
    assert code == 1
    record_id = model.record_id("synthetic", "POLICY-1")
    assert {"id": record_id, "reason": "source_unavailable"} in found["unfetched"]


def test_the_patch_text_is_never_printed(env, tmp_path):
    marker = "SYNTHETIC-MARKER-7f3a"
    hunk = f'@@ -1,3 +1,4 @@\n resource "x" "{marker}" {{\n+  message_retention_seconds = 1 # {marker}\n   name = "q"\n }}'
    code, found, text = check(env, tmp_path, diff({"infra/queue/main.tf": hunk}), raw=True)
    assert code == 1 and found["violations"]
    assert marker not in text and "message_retention_seconds" not in text


def test_line_numbers_follow_the_new_side_across_hunks(env, tmp_path):
    hunks = "\n".join(
        [
            "@@ -1,2 +1,2 @@",
            "-old = 1",
            "+new = 1",
            " keep = 2",
            "\\ No newline at end of file",
            "@@ -40,3 +40,4 @@",
            " a = 1",
            "-b = 2",
            "+b = 3",
            "+reply = requests.post(url)",
            " c = 4",
        ]
    )
    _, found = check(env, tmp_path, diff({"src/app/x.py": hunks}))
    assert [(v["path"], v["line"]) for v in found["violations"]] == [("src/app/x.py", 42)]


def test_a_deleted_file_is_not_checked(env, tmp_path):
    patch = "\n".join(
        [
            "diff --git a/src/app/old.py b/src/app/old.py",
            "deleted file mode 100644",
            "--- a/src/app/old.py",
            "+++ /dev/null",
            "@@ -1,1 +0,0 @@",
            "-reply = requests.get(url)",
            "",
        ]
    )
    code, found = check(env, tmp_path, patch)
    assert code == 0 and found["patch"]["files"] == 0


@pytest.mark.parametrize(
    "path",
    ["../outside.tf", "infra/../../outside.tf", "/etc/passwd", "a/./b.tf", "a\\b.tf"],
)
def test_a_patch_path_that_climbs_is_refused(env, tmp_path, path):
    patch = f"--- a/{path}\n+++ b/{path}\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    if path.startswith("/"):
        patch = f"--- {path}\n+++ {path}\n@@ -1,1 +1,1 @@\n-x\n+y\n"
    code, found = check(env, tmp_path, patch)
    assert (code, found) == (3, None)


@pytest.mark.parametrize(
    "patch",
    [
        b"--- a/x.tf\n+++ b/x.tf\n@@ -1,1 +1,1 @@\n-x\n+\x1b[31my\n",
        b"--- a/x.tf\n+++ b/x.tf\n@@ -1,1 +1,1 @@\n-x\n+y\x00\n",
        b"--- a/x.tf\n+++ b/x.tf\n@@ -1,1 +1,1 @@\n-x\n+\xff\xfe\n",
        b"@@ -1,1 +1,1 @@\n-x\n+y\n",
        b"--- a/x.tf\n+++ b/x.tf\n@@ -1,2 +1,2 @@\n-x\n+y\n",
        b'--- "a/q.tf"\n+++ "b/q\\tx.tf"\n@@ -1,1 +1,1 @@\n-x\n+y\n',
    ],
    ids=["escape", "nul", "not-utf8", "no-file-header", "short-hunk", "quoted-path"],
)
def test_a_malformed_patch_is_an_input_error(env, tmp_path, patch):
    assert check(env, tmp_path, patch) == (2, None)


def test_an_oversized_patch_is_refused_before_it_is_read_whole(env, tmp_path):
    line = "+" + "x" * 99 + "\n"
    patch = "--- a/x.tf\n+++ b/x.tf\n@@ -0,0 +1,30000 @@\n" + line * 30000
    assert check(env, tmp_path, patch) == (2, None)


def test_carriage_returns_of_a_windows_diff_are_read_as_line_ends(env, tmp_path):
    patch = BAD.replace("\n", "\r\n")
    code, found = check(env, tmp_path, patch)
    assert code == 1 and len(found["violations"]) == 2


def test_an_unknown_principal_is_refused(env, tmp_path):
    assert check(env, tmp_path, GOOD, task_path=task(tmp_path, principal="nobody")) == (3, None)


def test_an_index_built_with_another_config_stops(env, tmp_path):
    settings = json.loads(json.dumps(rr.CONFIG))
    settings["principals"]["workspace-c"] = settings["principals"]["workspace-b"]
    path = state_root(env) / "knowledge" / "config.json"
    paths.write_private(path, records.canonical(settings))
    code, found = check(env, tmp_path, BAD)
    assert code == 1 and found["status"] == "stale_index" and found["violations"] == []


def test_check_charges_nothing_to_the_task_total(env, tmp_path):
    check(env, tmp_path, BAD)
    assert not (state_root(env) / "knowledge" / "tasks").exists()


def test_check_writes_nothing_to_the_index_or_the_registry(env, tmp_path):
    root = state_root(env)

    def snapshot():
        folders = [root / "registry", root / "knowledge"]
        return {
            str(p.relative_to(root)): p.read_bytes()
            for folder in folders
            for p in sorted(folder.rglob("*"))
            if p.is_file() and not p.name.endswith(("-wal", "-shm"))
        }

    before = snapshot()
    check(env, tmp_path, BAD)
    assert snapshot() == before
