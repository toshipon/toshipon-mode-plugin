import json
import os
import subprocess
import sys
from pathlib import Path

from sre_harness import cli, paths
from sre_harness.knowledge import store
from tests.helpers import run_cli
from tests.knowledge_helpers import item, knowledge_dir, update, write_config, write_input

RUNTIME = Path(__file__).resolve().parents[2]
STATUS_KEYS = {
    "index",
    "generation",
    "schema_version",
    "sqlite_version",
    "fts5",
    "updated_at",
    "sources",
    "records",
}


def test_the_knowledge_group_is_registered(capsys):
    assert cli.main(["knowledge", "--help"]) == 0
    text = capsys.readouterr().out
    assert "status" in text and "update" in text
    assert cli.main(["knowledge", "search"]) == 2


def test_status_before_any_update(home):
    code, status = run_cli(["knowledge", "status"], home)
    assert code == 0
    assert set(status) == STATUS_KEYS
    assert status["index"] == "absent" and status["generation"] == 0
    assert status["fts5"] is True and status["sources"] == [] and status["records"] == {}
    assert status["sqlite_version"] == store.sqlite_version()
    assert not knowledge_dir(home).exists()


def test_update_then_status(home, tmp_path):
    write_config(home)
    path = write_input(tmp_path / "in.json", [item("R1"), item("R2", status="retired")])
    code, result = update(home, path)
    assert code == 0 and result["generation"] == {"before": 0, "after": 1}
    code, status = run_cli(["knowledge", "status"], home)
    assert code == 0 and set(status) == STATUS_KEYS
    assert status["index"] == "present" and status["generation"] == 1
    assert status["schema_version"] == store.SCHEMA_VERSION
    assert status["records"] == {"approved": 1, "retired": 1}
    (source,) = status["sources"]
    assert source["name"] == "file:synthetic" and source["state"] == "complete"
    assert source["cursor"].startswith("sha256:")


def test_status_names_no_project(home, tmp_path):
    write_config(home)
    update(home, write_input(tmp_path / "in.json", [item("R1")]))
    code, status = run_cli(["knowledge", "status"], home)
    text = json.dumps(status, ensure_ascii=False)
    assert code == 0
    for name in ("project-alpha", "team-alpha", "R1", "保持"):
        assert name not in text


def test_update_twice_does_not_advance(home, tmp_path):
    write_config(home)
    path = write_input(tmp_path / "in.json", [item("R1")])
    update(home, path)
    code, result = update(home, path)
    assert code == 0 and result["advanced"] is False


def test_dry_run_writes_nothing(home, tmp_path):
    write_config(home)
    path = write_input(tmp_path / "in.json", [item("R1")])
    code, result = update(home, path, "--dry-run")
    assert code == 0 and result["dry_run"] is True and result["counts"]["inserted"] == 1
    assert sorted(p.name for p in knowledge_dir(home).iterdir()) == ["config.json"]
    code, status = run_cli(["knowledge", "status"], home)
    assert status["generation"] == 0


def test_update_needs_an_input_for_the_file_source(home):
    write_config(home)
    code, _ = run_cli(["knowledge", "update", "--source", "file"], home)
    assert code == 2
    code, _ = run_cli(["knowledge", "update", "--source", "registry", "--input", "x"], home)
    assert code == 2


def test_update_without_fts5_exits_2_and_writes_nothing(home, tmp_path, monkeypatch, capsys):
    write_config(home)
    monkeypatch.setattr(store, "fts5_available", lambda: False)
    code, _ = update(home, write_input(tmp_path / "in.json", [item("R1")]))
    assert code == 2 and "FTS5" in capsys.readouterr().err
    assert not (knowledge_dir(home) / "index.sqlite3").exists()
    code, status = run_cli(["knowledge", "status"], home)
    assert code == 0 and status["fts5"] is False


def test_a_cycle_exits_2(home, tmp_path):
    write_config(home)
    cyclic = [
        item("R1", supersedes=[{"kind": "synthetic", "key": "R2"}]),
        item("R2", supersedes=[{"kind": "synthetic", "key": "R1"}]),
    ]
    code, _ = update(home, write_input(tmp_path / "in.json", cyclic))
    assert code == 2


def test_a_writer_in_another_process_is_refused_with_exit_1(home, tmp_path):
    write_config(home)
    path = write_input(tmp_path / "in.json", [item("R1")])
    update(home, path)
    root = paths.state_root(home)
    other = write_input(tmp_path / "other.json", [item("R2")], source="other")
    env = {**os.environ, "AI_HARNESS_HOME": home["AI_HARNESS_HOME"], "PYTHONPATH": str(RUNTIME)}
    argv = [
        sys.executable,
        "-m",
        "sre_harness",
        "knowledge",
        "update",
        "--source",
        "file",
        "--input",
        str(other),
    ]
    with store.writer(root):
        held = subprocess.run(
            argv, env=env, cwd=RUNTIME, capture_output=True, text=True, timeout=60
        )
    assert held.returncode == 1, held.stderr
    assert "writer" in held.stderr
    code, status = run_cli(["knowledge", "status"], home)
    assert status["generation"] == 1 and [s["name"] for s in status["sources"]] == [
        "file:synthetic"
    ]
    free = subprocess.run(argv, env=env, cwd=RUNTIME, capture_output=True, text=True, timeout=60)
    assert free.returncode == 0, free.stderr
    assert json.loads(free.stdout)["generation"] == {"before": 1, "after": 2}
