import copy
import json

import pytest

from sre_harness import paths, records
from sre_harness.errors import InputError
from sre_harness.knowledge import config, ingest, model, store
from sre_harness.knowledge.adapters import Changes, build
from sre_harness.knowledge.adapters import file as file_adapter
from tests.knowledge_helpers import CONFIG, item, source_file, write_config, write_input

NOW = "2026-11-27T00:00:00Z"
EVERYONE = config.principal(CONFIG, "workspace-a")


@pytest.fixture
def root(home):
    write_config(home)
    return paths.state_root(home)


def run(root, path, **options):
    return ingest.update(root, build("file", {"input": path}), now=NOW, **options)


def indexed(root):
    with store.reader(root) as reader:
        return {row["origin"]["key"]: row for row in reader.records(EVERYONE)}


# The file adapter ---------------------------------------------------------------------------


def test_the_file_adapter_returns_items_tombstones_cursor_and_completeness(tmp_path):
    stone = {"origin": {"kind": "synthetic", "key": "OLD"}, "reason": "deleted"}
    path = write_input(tmp_path / "in.json", [item("R1")], complete=False, tombstones=[stone])
    adapter = build("file", {"input": path})
    assert adapter.name == "file" and adapter.source == "file:synthetic"
    changes = adapter.changes(None)
    assert isinstance(changes, Changes)
    assert changes.items == [item("R1")] and changes.tombstones == [stone]
    assert changes.complete is False
    assert changes.cursor == "sha256:" + records.sha256(path.read_bytes())
    assert adapter.changes(changes.cursor) == changes


def test_the_file_adapter_needs_an_input(tmp_path):
    with pytest.raises(InputError, match="--input"):
        build("file", {"input": None})
    with pytest.raises(InputError, match="unknown source"):
        build("ftp", {})


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"schema_version": 1, "source": "x", "items": []}),
        json.dumps({**source_file([]), "source": "../escape"}),
        json.dumps(
            {
                **source_file([]),
                "tombstones": [{"origin": {"kind": "x", "key": "y"}, "reason": "whim"}],
            }
        ),
        json.dumps([]),
    ],
)
def test_a_malformed_input_file_is_an_input_error(tmp_path, content):
    path = tmp_path / "in.json"
    path.write_text(content)
    with pytest.raises(InputError):
        build("file", {"input": path}).changes(None)


def test_a_missing_or_oversized_input_is_an_input_error(tmp_path, monkeypatch):
    with pytest.raises(InputError, match="missing"):
        build("file", {"input": tmp_path / "absent.json"}).changes(None)
    path = write_input(tmp_path / "in.json", [item("R1")])
    monkeypatch.setattr(file_adapter, "MAX_BYTES", 10)
    with pytest.raises(InputError, match="larger than"):
        build("file", {"input": path}).changes(None)


# update -------------------------------------------------------------------------------------


def test_update_indexes_the_items_and_reports_counts(root, tmp_path):
    result = run(root, write_input(tmp_path / "in.json", [item("R1"), item("R2")]))
    assert result["source"] == "file:synthetic" and result["dry_run"] is False
    assert result["generation"] == {"before": 0, "after": 1} and result["advanced"] is True
    assert result["counts"] == {
        "items": 2,
        "indexed": 2,
        "inserted": 2,
        "updated": 0,
        "unchanged": 0,
        "tombstoned": 0,
        "rejected": 0,
        "duplicates": 0,
    }
    assert set(indexed(root)) == {"R1", "R2"}
    assert indexed(root)["R1"]["indexed_at"] == NOW


def test_update_without_a_config_is_an_input_error(home, tmp_path):
    root = paths.state_root(home)
    with pytest.raises(InputError, match="config.json"):
        run(root, write_input(tmp_path / "in.json", [item("R1")]))
    assert not store.exists(root)


def test_a_record_without_access_is_rejected_not_indexed(root, tmp_path):
    bare = item("R2")
    del bare["access"]
    result = run(root, write_input(tmp_path / "in.json", [item("R1"), bare]))
    assert [(r["reason"], r["origin"]) for r in result["rejected"]] == [
        ("access_missing", {"kind": "synthetic", "key": "R2"})
    ]
    assert result["rejected"][0]["id"] == model.record_id("synthetic", "R2")
    assert set(indexed(root)) == {"R1"}


def test_a_record_naming_an_unknown_reader_is_rejected(root, tmp_path):
    stranger = item(
        "R2", access={"classification": "internal", "readers": ["team-alpha", "team-x"]}
    )
    result = run(root, write_input(tmp_path / "in.json", [item("R1"), stranger]))
    (rejected,) = result["rejected"]
    assert rejected["reason"] == "unknown_reader" and "team-x" in rejected["detail"]
    assert set(indexed(root)) == {"R1"}


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "proposed", "approval": None, "enforcement": "mandatory"},
        {"status": "approved", "approval": None},
        {"status": "superseded", "approval": None},
        {
            "scope": {
                "level": "organization",
                "project_id": "p",
                "component_id": None,
                "task_id": None,
            }
        },
    ],
)
def test_a_record_breaking_an_invariant_is_rejected(root, tmp_path, changes):
    result = run(root, write_input(tmp_path / "in.json", [item("R1"), item("R2", **changes)]))
    assert [r["reason"] for r in result["rejected"]] == ["invariant_violated"]
    assert set(indexed(root)) == {"R1"}


def test_a_malformed_item_is_rejected_and_the_rest_indexed(root, tmp_path):
    broken = item("R2", status="maybe")
    no_origin = {"summary": "x"}
    result = run(root, write_input(tmp_path / "in.json", [item("R1"), broken, no_origin]))
    reasons = [(r["index"], r["reason"]) for r in result["rejected"]]
    assert reasons == [(1, "schema_invalid"), (2, "schema_invalid")]
    assert result["rejected"][1]["origin"] is None and result["rejected"][1]["id"] is None
    assert set(indexed(root)) == {"R1"}


def test_the_same_origin_twice_is_an_input_error_and_writes_nothing(root, tmp_path):
    with pytest.raises(InputError, match="twice"):
        run(root, write_input(tmp_path / "in.json", [item("R1"), item("R1", summary="別")]))
    assert not store.exists(root)


def test_the_same_content_from_two_origins_is_listed_not_merged(root, tmp_path):
    same = {"summary": "同じ文", "applies_when": []}
    result = run(
        root,
        write_input(tmp_path / "in.json", [item("R1", **same), item("R2", **same), item("R3")]),
    )
    (duplicate,) = result["duplicates"]
    assert duplicate["ids"] == sorted(model.record_id("synthetic", k) for k in ("R1", "R2"))
    assert set(indexed(root)) == {"R1", "R2", "R3"}


def test_a_record_now_rejected_is_taken_out(root, tmp_path):
    run(root, write_input(tmp_path / "in.json", [item("R1"), item("R2")]))
    stranger = item("R2", access={"classification": "internal", "readers": ["team-x"]})
    result = run(root, write_input(tmp_path / "in.json", [stranger], complete=False))
    assert result["tombstoned"] == [
        {"id": model.record_id("synthetic", "R2"), "reason": "rejected"}
    ]
    assert set(indexed(root)) == {"R1"}


def test_a_second_identical_update_does_not_advance(root, tmp_path):
    path = write_input(tmp_path / "in.json", [item("R1"), item("R2")])
    run(root, path)
    again = run(root, path)
    assert again["advanced"] is False and again["generation"] == {"before": 1, "after": 1}
    assert again["counts"]["unchanged"] == 2


def test_a_config_change_advances_the_next_update(root, tmp_path, home):
    path = write_input(tmp_path / "in.json", [item("R1")])
    run(root, path)
    changed = copy.deepcopy(CONFIG)
    changed["principals"]["workspace-b"]["readers"].append("team-gamma")
    write_config(home, changed)
    result = run(root, path)
    assert result["advanced"] is True and result["config_changed"] is True
    assert result["generation"] == {"before": 1, "after": 2}


def test_partial_and_complete_updates(root, tmp_path):
    run(root, write_input(tmp_path / "in.json", [item("R1"), item("R2")]))
    partial = run(root, write_input(tmp_path / "in.json", [item("R1")], complete=False))
    assert partial["tombstoned"] == [] and set(indexed(root)) == {"R1", "R2"}
    complete = run(root, write_input(tmp_path / "in.json", [item("R1")]))
    assert complete["tombstoned"] == [
        {"id": model.record_id("synthetic", "R2"), "reason": "removed_from_source"}
    ]
    assert set(indexed(root)) == {"R1"}


def test_an_explicit_tombstone_from_the_source(root, tmp_path):
    run(root, write_input(tmp_path / "in.json", [item("R1"), item("R2")]))
    stone = {"origin": {"kind": "synthetic", "key": "R2"}, "reason": "deleted"}
    result = run(root, write_input(tmp_path / "in.json", [], complete=False, tombstones=[stone]))
    assert result["tombstoned"] == [{"id": model.record_id("synthetic", "R2"), "reason": "deleted"}]


def test_a_supersession_cycle_stops_the_whole_update(root, tmp_path):
    run(root, write_input(tmp_path / "in.json", [item("R1")]))
    cyclic = [
        item("R1", supersedes=[{"kind": "synthetic", "key": "R2"}]),
        item("R2", supersedes=[{"kind": "synthetic", "key": "R1"}]),
    ]
    with pytest.raises(InputError, match="cycle"):
        run(root, write_input(tmp_path / "in.json", cyclic))
    assert set(indexed(root)) == {"R1"}
    with store.reader(root) as reader:
        assert reader.generation == 1


def test_dry_run_reports_the_plan_and_writes_nothing(root, tmp_path):
    path = write_input(tmp_path / "in.json", [item("R1")])
    first = run(root, path, dry_run=True)
    assert first["dry_run"] is True and first["advanced"] is True
    assert first["generation"] == {"before": 0, "after": 1}
    assert not store.exists(root)
    assert sorted(p.name for p in (root / "knowledge").iterdir()) == ["config.json"]
    run(root, path)
    database = (root / "knowledge" / "index.sqlite3").read_bytes()
    second = run(
        root, write_input(tmp_path / "in.json", [item("R1", summary="変えた")]), dry_run=True
    )
    assert second["counts"]["updated"] == 1 and second["generation"] == {"before": 1, "after": 2}
    assert (root / "knowledge" / "index.sqlite3").read_bytes() == database
    assert indexed(root)["R1"]["summary"] != "変えた"


def test_dry_run_also_refuses_a_cycle(root, tmp_path):
    cyclic = [
        item("R1", supersedes=[{"kind": "synthetic", "key": "R2"}]),
        item("R2", supersedes=[{"kind": "synthetic", "key": "R1"}]),
    ]
    with pytest.raises(InputError, match="cycle"):
        run(root, write_input(tmp_path / "in.json", cyclic), dry_run=True)


def test_the_adapter_is_given_the_cursor_it_returned_last_time(root, tmp_path):
    seen = []

    class Recording:
        name = "file"
        source = "file:recording"

        def changes(self, cursor):
            seen.append(cursor)
            return Changes(items=[item("R1")], tombstones=[], cursor=f"c{len(seen)}", complete=True)

    ingest.update(root, Recording(), now=NOW)
    ingest.update(root, Recording(), now=NOW)
    assert seen == [None, "c1"]
