"""README の「最初に試す手順」の Knowledge Retrieval: the same commands, through `cli.main`, give
the exit codes and the values the README quotes."""

import json

import pytest

from examples.synthetic import knowledge_corpus as kc
from examples.synthetic import review_registry as rr
from sre_harness import paths, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import ingest
from tests.helpers import run_cli

HEAD = "1" * 40
INDEXED = "2026-11-27T00:00:00Z"
TASK = {
    "schema_version": 1,
    "task_id": "TASK-0101",
    "workspace_id": "ws-a",
    "principal": "workspace-a",
    "project_id": "project-alpha",
    "component_id": "queue",
    "repo": "example-org/alpha",
    "base_sha": None,
    "changed_paths": ["services/queue/main.tf", "infra/queue/main.tf"],
    "resource_types": ["queue"],
}
CHANGE = """diff --git a/infra/queue/main.tf b/infra/queue/main.tf
--- a/infra/queue/main.tf
+++ b/infra/queue/main.tf
@@ -1,3 +1,4 @@
 resource "example_queue" "q" {
+  message_retention_seconds = 86400
   name = "q"
 }
"""


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """The README's steps run on one clock: the updates at 00:00, the rest at 06:00. With the
    real clock for the updates, a run after 06:00 of that day would index the records after the
    search's clock, and their freshness would turn `unknown`."""
    monkeypatch.setattr(knowledge_cli, "clock", lambda: "2026-11-27T06:00:00Z")
    real = ingest.records.iso
    monkeypatch.setattr(
        ingest.records, "iso", lambda moment=None: real(moment) if moment else INDEXED
    )


def test_the_first_steps_of_knowledge_retrieval(home, tmp_path):
    root = paths.state_root(home)
    written = kc.write_scenario(tmp_path / "knowledge", 300)
    kc.write_sources(root, kc.scenario(300))
    config = json.loads((tmp_path / "knowledge" / "config.json").read_text(encoding="utf-8"))
    paths.write_private(root / "knowledge" / "config.json", records.canonical(config))
    rr.write(root)
    update = ["knowledge", "update", "--source"]
    assert run_cli([*update, "file", "--input", written["corpus"]], home)[0] == 0
    code, registry = run_cli([*update, "review-registry"], home)
    assert code == 0 and registry["counts"]["indexed"] == 5
    assert {row["entry"]: row["kind"] for row in registry["skipped"]} == {
        "RG-9006": "unmapped",
        "RG-9007": "invalid",
    }
    task = tmp_path / "task.json"
    task.write_text(json.dumps(TASK, ensure_ascii=False))
    change = tmp_path / "change.diff"
    change.write_text(CHANGE)

    code, found = run_cli(
        ["knowledge", "context", "--task-file", str(task), "--query", "queue の保持期間"], home
    )
    assert (code, found["status"], found["budget"]["used_tokens"]) == (0, "ok", 1351)
    assert (found["mandatory"]["count"], found["mandatory"]["tokens"]) == (3, 685)
    assert (len(found["records"]), found["truncated"]["count"]) == (6, 24)
    assert "KN-904a3334f940" in [shown["id"] for shown in found["records"]]

    code, fetched = run_cli(
        ["knowledge", "fetch", "--principal", "workspace-a", "--id", "KN-904a3334f940"]
        + ["--task", "TASK-0101"],
        home,
    )
    assert (code, fetched["status"], fetched["budget"]["used_tokens"]) == (0, "ok", 65)
    assert fetched["budget"]["task"]["used_after"] == 1416
    assert fetched["fragments"][0]["locator"] == "registry:RG-0149/v0001.md"

    code, checked = run_cli(
        ["knowledge", "check", "--task-file", str(task), "--patch", str(change)], home
    )
    assert code == 1 and checked["status"] == "violations"
    assert [(v["id"], v["path"], v["line"]) for v in checked["violations"]] == [
        ("KN-7e348b3581d2", "infra/queue/main.tf", 2)
    ]
    assert {row["reason"] for row in checked["unfetched"]} == {"source_unavailable"}

    ask = ["--question", "保持期間を 1 日にする変更を例外として認めるか"]
    ask += ["--option", "認める", "--option", "認めない", "--evidence", "KN-7e348b3581d2"]
    ask += ["--risk", "high", "--head-sha", HEAD]
    code, put = run_cli(["knowledge", "decision", "put", "--task-file", str(task), *ask], home)
    assert (code, put["status"], put["request"]["id"]) == (0, "accepted", "DR-fa9e9270fe32")
    code, polled = run_cli(
        ["knowledge", "decision", "poll", "--workspace", "ws-a", "--head-sha", HEAD], home
    )
    assert code == 0 and polled["responses"] == []
    assert [(r["id"], r["status"]) for r in polled["requests"]] == [("DR-fa9e9270fe32", "open")]
