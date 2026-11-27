"""`knowledge migrate` and `knowledge rollback`: back up, take the registry in on a copy, check
it against `registry.active_rules`, put it in place in one rename; and go back."""

import os
import stat

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import paths, records
from sre_harness.errors import InputError, QualityGateError
from sre_harness.knowledge import config, ingest, migrate, model, store
from sre_harness.knowledge.adapters import build
from tests.knowledge_helpers import item, write_input

NOW = "2026-11-27T00:00:00Z"
LATER = "2026-11-27T01:00:00Z"
ORIGIN = "review-registry"


@pytest.fixture
def root(tmp_path):
    state = paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "state")})
    paths.write_private(state / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    return state


@pytest.fixture
def made(root):
    return rr.write(root)


@pytest.fixture
def indexed(root, tmp_path):
    """An index holding one record of another source (a file), at generation 1."""
    path = write_input(tmp_path / "in.json", [item("R1")])
    ingest.update(root, build("file", {"input": path}), now=NOW)
    return model.record_id("synthetic", "R1")


def tree(root):
    """{relative path: bytes} of every file under knowledge/."""
    base = root / "knowledge"
    return {
        str(path.relative_to(base)): path.read_bytes()
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def everything(root):
    settings = config.load(root)
    principal = config.Principal(
        "all", config.known_readers(settings), frozenset(config.CLASSIFICATIONS)
    )
    with store.reader(root) as reader:
        return reader.generation, {row["id"]: row for row in reader.records(principal)}


def key(entry):
    return model.record_id(ORIGIN, entry)


# The check ---------------------------------------------------------------------------------


def test_the_check_compares_entry_sets_per_repo():
    found = migrate.differences(
        {"example-org/a": {"RG-0001", "RG-0002"}, "example-org/b": set()},
        {"example-org/a": {"RG-0001", "RG-0003"}},
    )
    assert found == [
        {
            "repo": "example-org/a",
            "ok": False,
            "registry": ["RG-0001", "RG-0002"],
            "index": ["RG-0001", "RG-0003"],
            "missing": ["RG-0002"],
            "unexpected": ["RG-0003"],
            "error": None,
        },
        {
            "repo": "example-org/b",
            "ok": True,
            "registry": [],
            "index": [],
            "missing": [],
            "unexpected": [],
            "error": None,
        },
    ]


def test_a_repo_the_registry_refuses_is_a_difference():
    (found,) = migrate.differences({"example-org/a": "artifact changed"}, {})
    assert found["ok"] is False and found["error"] == "artifact changed"


# Dry run -----------------------------------------------------------------------------------


def test_a_dry_run_lists_the_plan_and_writes_nothing(root, made):
    before = tree(root)
    result = migrate.plan(root, now=NOW)
    assert tree(root) == before and not store.exists(root)
    assert result["dry_run"] is True and result["replaced"] is False
    planned = {row["entry"]: row for row in result["plan"]["records"]}
    assert sorted(planned) == ["RG-9001", "RG-9002", "RG-9003", "RG-9004", "RG-9005"]
    assert planned["RG-9001"] == {
        "entry": "RG-9001",
        "version": 1,
        "id": key("RG-9001"),
        "kind": "rule",
        "status": "approved",
        "scope": {
            "level": "project",
            "project_id": "project-alpha",
            "component_id": None,
            "task_id": None,
        },
        "access": {"classification": "internal", "readers": ["team-alpha"]},
        "enforcement": "advisory",
    }
    assert [row["entry"] for row in result["plan"]["unmapped"]] == ["RG-9006"]
    assert [row["entry"] for row in result["plan"]["invalid"]] == ["RG-9007"]
    assert result["generation"] == {"before": 0, "after": 1}
    assert result["backup"] == {"name": None, "reason": "dry_run"}


def test_a_dry_run_over_an_index_writes_nothing_either(root, made, indexed):
    before = tree(root)
    result = migrate.plan(root, now=NOW)
    assert tree(root) == before
    assert result["generation"] == {"before": 1, "after": 2}
    assert result["changes"]["inserted"] == sorted(
        key(entry) for entry in ("RG-9001", "RG-9002", "RG-9003", "RG-9004", "RG-9005")
    )


def test_a_dry_run_after_a_search_left_its_wal_writes_nothing(root, made, indexed):
    with store.reader(root) as reader:
        reader.meta()  # leaves the -wal and -shm files a reader opens
    before = tree(root)
    migrate.plan(root, now=NOW)
    assert tree(root) == before


# Running -----------------------------------------------------------------------------------


def test_a_migration_backs_up_takes_the_registry_in_and_keeps_other_sources(root, made, indexed):
    result = migrate.run(root, now=NOW)
    assert migrate.exit_code(result) == 0 and result["replaced"] is True
    assert result["check"]["ok"] is True
    assert result["generation"] == {"before": 1, "after": 2}
    backup = result["backup"]
    assert store.BACKUP_NAME.fullmatch(backup["name"]) and backup["generation"] == 1
    generation, rows = everything(root)
    assert generation == 2 and indexed in rows  # the file source's record is kept
    migrated = {row["origin"]["key"] for row in rows.values() if row["origin"]["kind"] == ORIGIN}
    assert migrated == {"RG-9001", "RG-9002", "RG-9003", "RG-9004", "RG-9005"}
    assert {row["enforcement"] for row in rows.values()} == {"advisory"}


def test_the_check_holds_per_repo_after_a_migration(root, made):
    result = migrate.run(root, now=NOW)
    repos = {row["repo"]: row for row in result["check"]["repos"]}
    assert sorted(repos) == [rr.ALPHA, rr.GAMMA]
    assert repos[rr.ALPHA]["registry"] == repos[rr.ALPHA]["index"] == ["RG-9001", "RG-9002"]
    assert repos[rr.GAMMA]["index"] == ["RG-9005"]


def test_the_backup_is_private_and_a_full_copy(root, made, indexed):
    result = migrate.run(root, now=NOW)
    path = root / "knowledge" / "backups" / result["backup"]["name"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    listed = migrate.backups(root)
    assert [entry["name"] for entry in listed] == [result["backup"]["name"]]
    assert listed[0]["generation"] == 1 and listed[0]["created_at"] == NOW


def test_without_an_index_there_is_nothing_to_back_up(root, made):
    result = migrate.run(root, now=NOW)
    assert result["backup"] == {"name": None, "reason": "no_index"}
    assert result["generation"] == {"before": 0, "after": 1}
    assert migrate.backups(root) == []


def test_the_replaced_index_leaves_no_wal_frames_behind(root, made, indexed):
    migrate.run(root, now=NOW)
    folder = root / "knowledge"
    wal = folder / (store.INDEX + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0
    assert not [name for name in os.listdir(folder) if name.startswith(".staged-")]


def test_an_entry_the_registry_refuses_stops_the_migration_and_changes_nothing(root, indexed):
    rr.write(root, tampered=True)
    before = tree(root)
    result = migrate.run(root, now=NOW)
    assert migrate.exit_code(result) == 1 and result["replaced"] is False
    assert result["check"]["ok"] is False
    (alpha,) = [row for row in result["check"]["repos"] if row["repo"] == rr.ALPHA]
    assert alpha["error"] and "RG-9008" in alpha["error"]
    assert result["backup"] == {"name": None, "reason": "not_replaced"}
    assert tree(root) == before
    assert everything(root)[0] == 1


def test_a_difference_found_by_the_check_changes_nothing(root, made, indexed, monkeypatch):
    real = migrate.expected

    def one_more(root, settings):
        found = real(root, settings)
        found[rr.ALPHA] = found[rr.ALPHA] | {"RG-0042"}
        return found

    monkeypatch.setattr(migrate, "expected", one_more)
    before = tree(root)
    result = migrate.run(root, now=NOW)
    assert migrate.exit_code(result) == 1 and result["replaced"] is False
    (alpha,) = [row for row in result["check"]["repos"] if row["repo"] == rr.ALPHA]
    assert alpha["missing"] == ["RG-0042"]
    assert tree(root) == before


def test_an_error_while_building_changes_nothing(root, made, indexed, monkeypatch):
    def broken(self, batch, now):
        raise RuntimeError("stopped half way")

    monkeypatch.setattr(store.Writer, "apply", broken)
    before = tree(root)
    with pytest.raises(RuntimeError):
        migrate.run(root, now=NOW)
    assert tree(root) == before
    assert everything(root)[0] == 1


def test_a_held_writer_lock_stops_migrate_and_rollback(root, made, indexed):
    first = migrate.run(root, now=NOW)
    with store.writer(root):
        before = tree(root)
        with pytest.raises(QualityGateError):
            migrate.run(root, now=LATER)
        with pytest.raises(QualityGateError):
            migrate.rollback(root, first["backup"]["name"], now=LATER)
        with pytest.raises(QualityGateError):
            migrate.plan(root, now=LATER)
        assert tree(root) == before


def test_a_second_migration_with_nothing_new_keeps_the_generation(root, made):
    migrate.run(root, now=NOW)
    again = migrate.run(root, now=LATER)
    assert again["generation"] == {"before": 1, "after": 1} and again["replaced"] is True


def test_two_backups_of_one_generation_in_the_same_second_get_different_names(root, made, indexed):
    migrate.run(root, now=NOW)  # backs up generation 1, makes 2
    second = migrate.run(root, now=NOW)  # backs up generation 2, nothing new
    third = migrate.run(root, now=NOW)  # backs up generation 2 again
    assert second["backup"]["generation"] == third["backup"]["generation"] == 2
    assert second["backup"]["name"] != third["backup"]["name"]
    listed = migrate.backups(root)
    assert len(listed) == 3 and all(store.BACKUP_NAME.fullmatch(e["name"]) for e in listed)


# Rollback ----------------------------------------------------------------------------------


def test_a_rollback_restores_the_backup_and_moves_the_generation_on(root, made, indexed):
    _, before = everything(root)
    migrated = migrate.run(root, now=NOW)
    result = migrate.rollback(root, migrated["backup"]["name"], now=LATER)
    assert result["restored"] == {"name": migrated["backup"]["name"], "generation": 1}
    assert result["generation"] == {"before": 2, "after": 3}
    assert store.BACKUP_NAME.fullmatch(result["backup"]["name"])
    assert result["backup"]["generation"] == 2
    generation, rows = everything(root)
    assert generation == 3 and rows == before


def test_a_rollback_can_itself_be_rolled_back(root, made, indexed):
    migrated = migrate.run(root, now=NOW)
    _, after = everything(root)
    back = migrate.rollback(root, migrated["backup"]["name"], now=LATER)
    migrate.rollback(root, back["backup"]["name"], now=LATER)
    generation, rows = everything(root)
    assert generation == 4 and rows == after


@pytest.mark.parametrize(
    "name",
    [
        "../index.sqlite3",
        "index-20261127T000000Z-g0001.sqlite3/../../x",
        "/etc/passwd",
        "index.sqlite3",
        "index-20261127T000000Z-g0001.sqlite3-wal",
        "",
    ],
)
def test_a_rollback_takes_only_a_name_the_harness_made(root, made, indexed, name):
    before = tree(root)
    with pytest.raises(InputError):
        migrate.rollback(root, name, now=LATER)
    assert tree(root) == before


def test_a_rollback_to_a_missing_backup_is_an_input_error(root, made, indexed):
    with pytest.raises(InputError, match="no backup"):
        migrate.rollback(root, "index-20261127T000000Z-g0001.sqlite3", now=LATER)


def test_a_backup_that_is_no_index_is_refused_and_changes_nothing(root, made, indexed):
    name = "index-20261127T000000Z-g0001.sqlite3"
    paths.write_private(root / "knowledge" / "backups" / name, "not a database\n")
    before = tree(root)
    with pytest.raises(InputError, match="no knowledge index"):
        migrate.rollback(root, name, now=LATER)
    assert tree(root) == before


def test_a_backup_of_another_schema_version_is_refused(root, made, indexed):
    import sqlite3

    migrated = migrate.run(root, now=NOW)
    path = root / "knowledge" / "backups" / migrated["backup"]["name"]
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("UPDATE meta SET value = '99' WHERE key = 'schema_version'")
    connection.close()
    before = tree(root)
    with pytest.raises(InputError, match="schema version 99"):
        migrate.rollback(root, migrated["backup"]["name"], now=LATER)
    assert tree(root) == before


def test_the_backup_list_holds_only_names_the_harness_makes(root, made, indexed):
    migrated = migrate.run(root, now=NOW)
    folder = root / "knowledge" / "backups"
    paths.write_private(folder / "notes.txt", "x")
    (folder / "index-20261127T000000Z-g0009.sqlite3").symlink_to(folder / "notes.txt")
    (folder / "index-20261127T000000Z-g0008.sqlite3").mkdir()
    assert [entry["name"] for entry in migrate.backups(root)] == [migrated["backup"]["name"]]
