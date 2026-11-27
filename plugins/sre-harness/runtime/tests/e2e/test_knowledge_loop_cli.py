"""The execution and learning loop end to end, through `cli.main`, on synthetic data: two
projects, three workspaces, two principals, one index and one decision queue.

    ws-alpha-1  principal workspace-b  project-alpha  TASK-ALPHA
    ws-alpha-2  principal workspace-a  project-alpha  TASK-ALPHA (the same task)
    ws-gamma    principal workspace-a  project-gamma  TASK-GAMMA

workspace-b cannot read project-gamma. Each step is one of the six of the loop: context (1),
check (3), decision put (4), answer and poll (5).
"""

import io
import json

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import cli, paths, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import decisions, model
from sre_harness.review import registry
from tests.knowledge_helpers import project_block, registry_entry, state_root

NOW = "2026-11-27T06:00:00Z"
HEAD, MOVED = "1" * 40, "2" * 40
QUEUE_BAD = """\
diff --git a/infra/queue/main.tf b/infra/queue/main.tf
--- a/infra/queue/main.tf
+++ b/infra/queue/main.tf
@@ -1,3 +1,4 @@
 resource "example_queue" "q" {
+  message_retention_seconds = 86400
   name = "q"
 }
"""
EVAL_CHECK = {"path_globs": ["src/**/*.py"], "added_line_pattern": "\\beval\\(", "message": "x"}
QUESTION = "保持期間を 1 日にする変更を例外として認めるか"
OTHER_QUESTION = "保持期間の既定値を設定ファイルに移すか"


def rid(entry):
    return model.record_id("review-registry", entry)


def knowledge(env, *argv):
    """(exit code, the JSON printed last, all of stdout, stderr is not read)."""
    out = io.StringIO()
    code = cli.main(["knowledge", *argv], env=env, out=out)
    text = out.getvalue()
    start = 0 if text.startswith("{") else text.rfind("\n{\n") + 1
    value = json.loads(text[start:]) if text.strip() and (start or text.startswith("{")) else None
    return code, value, text


@pytest.fixture
def loop(home, tmp_path, monkeypatch):
    monkeypatch.setattr(knowledge_cli, "clock", lambda: NOW)
    root = state_root(home)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    queue = rr.text("キューの保持期間は 14 日にする", "保持期間を明示する。", rr.QUEUE_CHECK)
    prose = rr.text("ログには要求 ID を残す", "文章のルール。")
    evaluated = rr.text("eval を使わない", "宣言的検査。", EVAL_CHECK)
    mandatory = project_block("project-alpha", "mandatory", ["team-alpha"])
    registry_entry(root, "RG-0001", rr.ALPHA, [("approved", queue)], knowledge=mandatory)
    registry_entry(root, "RG-0002", rr.ALPHA, [("approved", prose)])
    registry_entry(root, "RG-0003", rr.GAMMA, [("approved", evaluated)])
    assert knowledge(home, "update", "--source", "review-registry")[0] == 0
    tasks = {}
    for workspace, principal, project, repo, task in (
        ("ws-alpha-1", "workspace-b", "project-alpha", rr.ALPHA, "TASK-ALPHA"),
        ("ws-alpha-2", "workspace-a", "project-alpha", rr.ALPHA, "TASK-ALPHA"),
        ("ws-gamma", "workspace-a", "project-gamma", rr.GAMMA, "TASK-GAMMA"),
    ):
        path = tmp_path / f"{workspace}.json"
        value = {
            "schema_version": 1,
            "task_id": task,
            "workspace_id": workspace,
            "principal": principal,
            "project_id": project,
            "component_id": None,
            "repo": repo,
            "base_sha": HEAD,
            "changed_paths": ["infra/queue/main.tf"] if project == "project-alpha" else [],
            "resource_types": [],
        }
        path.write_text(json.dumps(value))
        tasks[workspace] = path
    patch = tmp_path / "change.diff"
    patch.write_text(QUEUE_BAD)
    monkeypatch.setattr(knowledge_cli, "_is_terminal", lambda stream: True)
    return home, root, tasks, patch


def put(env, task_path, question=QUESTION, evidence=(rid("RG-0001"),)):
    argv = ["decision", "put", "--task-file", str(task_path), "--question", question]
    argv += ["--option", "認める", "--option", "認めない", "--risk", "high", "--head-sha", HEAD]
    for record_id in evidence:
        argv += ["--evidence", record_id]
    return knowledge(env, *argv)


def answer(env, root, monkeypatch, request_id, option="認めない"):
    request = json.loads(
        (root / "knowledge" / "decisions" / "outbox" / f"{request_id}.json").read_text()
    )
    typed = decisions.confirmation(request, option, {"scope": "task", "until": None})
    monkeypatch.setattr("builtins.input", lambda prompt: typed[: decisions.CONFIRM_LENGTH])
    argv = ["decision", "answer", "--request", request_id, "--option", option]
    return knowledge(env, *argv, "--responder", "alice")


def poll(env, workspace, head=HEAD):
    code, value, _ = knowledge(
        env, "decision", "poll", "--workspace", workspace, "--head-sha", head
    )
    assert records.violations("decision-request", value, "poll_result") == [], value
    return code, value


def snapshot(root):
    """The bytes of the index file and of every registry file."""
    files = [root / "knowledge" / "index.sqlite3", *sorted((root / "registry").rglob("*"))]
    return {str(path.relative_to(root)): path.read_bytes() for path in files if path.is_file()}


def test_the_loop_across_workspaces_projects_and_principals(loop, monkeypatch, capsys):
    env, root, tasks, patch = loop

    # 1. each workspace gets the knowledge of its own project only
    code, alpha, _ = knowledge(env, "context", "--task-file", str(tasks["ws-alpha-1"]))
    assert code == 0 and records.violations("knowledge-search-result", alpha) == []
    assert {r["id"] for r in alpha["records"]} == {rid("RG-0001"), rid("RG-0002")}
    code, gamma, _ = knowledge(env, "context", "--task-file", str(tasks["ws-gamma"]))
    assert code == 0 and {r["id"] for r in gamma["records"]} == {rid("RG-0003")}

    # 3. the alpha workspace's patch breaks the mandatory retention check
    argv = ["check", "--task-file", str(tasks["ws-alpha-1"]), "--patch", str(patch)]
    code, checked, _ = knowledge(env, *argv)
    assert code == 1
    assert [(v["id"], v["enforcement"], v["line"]) for v in checked["violations"]] == [
        (rid("RG-0001"), "mandatory", 2)
    ]
    assert checked["not_checked"] == [{"id": rid("RG-0002"), "reason": "prose"}]

    # 4. it asks a person; the other workspace on the same task asks the same: one request
    code, first, _ = put(env, tasks["ws-alpha-1"])
    assert code == 0 and first["status"] == "accepted"
    request_id = first["request"]["id"]
    code, second, _ = put(env, tasks["ws-alpha-2"])
    assert code == 0 and second["status"] == "duplicate"
    assert second["request"]["id"] == request_id
    assert len(list((root / "knowledge" / "decisions" / "outbox").iterdir())) == 1

    # a workspace that cannot read project-gamma cannot cite it, and cannot tell it exists
    capsys.readouterr()
    hidden = put(env, tasks["ws-alpha-1"], OTHER_QUESTION, (rid("RG-0003"),))
    hidden_error = capsys.readouterr().err
    absent = put(env, tasks["ws-alpha-1"], OTHER_QUESTION, ("KN-000000000000",))
    absent_error = capsys.readouterr().err
    assert hidden[:2] == absent[:2] == (2, None)
    assert hidden_error.replace(rid("RG-0003"), "<id>") == absent_error.replace(
        "KN-000000000000", "<id>"
    )

    # the gamma workspace sees no request of the alpha task
    code, seen = poll(env, "ws-gamma")
    assert code == 0 and seen["requests"] == []

    # 5. a person answers; the answer changes neither the index nor the registry
    before = snapshot(root)
    code, response, text = answer(env, root, monkeypatch, request_id)
    assert code == 0 and response["option"] == "認めない" and QUESTION in text
    assert snapshot(root) == before

    code, delivered = poll(env, "ws-alpha-1")
    assert code == 0 and [r["request_id"] for r in delivered["responses"]] == [request_id]
    code, again = poll(env, "ws-alpha-1")
    assert code == 0 and again["responses"] == []
    assert [r["request_id"] for r in again["already_delivered"]] == [request_id]
    code, other = poll(env, "ws-alpha-2")
    assert code == 0 and [r["request_id"] for r in other["responses"]] == [request_id]

    # a second question is answered, then the evidence moves before it is delivered
    code, later, _ = put(env, tasks["ws-alpha-1"], OTHER_QUESTION)
    later_id = later["request"]["id"]
    assert answer(env, root, monkeypatch, later_id, "認める")[0] == 0
    code, moved = poll(env, "ws-alpha-1", MOVED)
    assert code == 1 and moved["status"] == "stale_response"
    assert moved["stale"] == [{"request_id": later_id, "reasons": ["head_sha_changed"]}]
    assert moved["responses"] == []

    queue = rr.text("キューの保持期間は 14 日にする", "保持期間を明示する。", rr.QUEUE_CHECK)
    block = project_block("project-alpha", "mandatory", ["team-alpha"])
    block["applies_when"] = ["キューを新しく作る"]
    version = rr.version("RG-0001", 2, "approved", rr.ALPHA, queue, 1)
    version["knowledge"] = block
    registry.write_version(root, version, queue)
    assert knowledge(env, "update", "--source", "review-registry")[0] == 0
    code, changed = poll(env, "ws-alpha-1")
    assert code == 1 and changed["stale"] == [
        {"request_id": later_id, "reasons": ["content_hash_changed"]}
    ]
    assert changed["responses"] == []
    assert [r["request_id"] for r in changed["already_delivered"]] == [request_id]

    # the queue as an operator sees it
    code, listed, _ = knowledge(env, "decision", "list")
    status = {row["id"]: row["status"] for row in listed["requests"]}
    assert status == {request_id: "answered", later_id: "answered"}
