import os
import sqlite3
import stat
import threading

import pytest

from sre_harness import paths
from sre_harness.errors import BoundaryError, InputError, QualityGateError
from sre_harness.knowledge import config, model, store
from tests.knowledge_helpers import CONFIG, item

NOW = "2026-11-27T00:00:00Z"
LATER = "2026-11-28T00:00:00Z"
EVERYONE = config.principal(CONFIG, "workspace-a")
LIMITED = config.principal(CONFIG, "workspace-b")


@pytest.fixture
def root(home):
    return paths.state_root(home)


def rec(key, **changes):
    return model.normalize(item(key, **changes), NOW)


def batch(
    records,
    *,
    source="file:synthetic",
    complete=True,
    tombstones=(),
    rejected=(),
    config_hash="c1",
    cursor="k1",
):
    return store.Batch(
        source=source,
        adapter="file",
        cursor=cursor,
        complete=complete,
        records=list(records),
        tombstones=list(tombstones),
        rejected=list(rejected),
        config_hash=config_hash,
    )


def apply(root, value, now=NOW):
    with store.writer(root) as writer:
        return writer.apply(value, now)


def snapshot(root, principal=EVERYONE):
    with store.reader(root) as reader:
        return reader.generation, {
            row["id"]: row["content_hash"] for row in reader.records(principal)
        }


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


# FTS5 -----------------------------------------------------------------------------------


def test_fts5_is_available_in_this_python():
    """Not skipped anywhere: CI runs this on ubuntu (3.10, 3.13) and macOS (3.13)."""
    assert store.fts5_available() is True
    store.require_fts5()


def test_a_two_character_japanese_word_hits(root):
    apply(
        root,
        batch(
            [
                rec("R1", summary="キューのメッセージ保持期間は 14 日にする"),
                rec("R2", summary="timeout を 10 秒にする"),
            ]
        ),
    )
    with store.reader(root) as reader:
        hits = reader.matching(EVERYONE, ["保持"])
        assert [row_id for row_id, _ in hits] == [model.record_id("synthetic", "R1")]
        assert reader.matching(EVERYONE, ["timeout"])[0][0] == model.record_id("synthetic", "R2")
        assert reader.matching(EVERYONE, ["存在しない"]) == []
        assert reader.matching(EVERYONE, []) == []


def test_query_terms_are_bound_not_parsed(root):
    apply(root, batch([rec("R1")]))
    with store.reader(root) as reader:
        for hostile in ['"', 'a" OR "b', "NEAR(a b)", "summary:x", "*", "') --"]:
            assert reader.matching(EVERYONE, [hostile]) == []


def test_no_fts5_is_an_input_error_with_the_reason(root, monkeypatch):
    monkeypatch.setattr(store, "fts5_available", lambda: False)
    with pytest.raises(InputError, match="FTS5") as stopped:
        store.require_fts5()
    assert stopped.value.exit_code == 2
    with pytest.raises(InputError, match="FTS5"):
        with store.writer(root):
            pass
    assert not (root / "knowledge" / "index.sqlite3").exists()


# Files and modes --------------------------------------------------------------------------


def test_the_index_is_private_and_in_wal_mode(root):
    apply(root, batch([rec("R1")]))
    directory = root / "knowledge"
    assert mode(directory) == 0o700
    for name in ("index.sqlite3", "writer.lock"):
        assert mode(directory / name) == 0o600
    for extra in directory.glob("index.sqlite3-*"):
        assert mode(extra) == 0o600
    connection = sqlite3.connect(directory / "index.sqlite3")
    try:
        assert connection.execute("pragma journal_mode").fetchone()[0] == "wal"
        tables = {row[0] for row in connection.execute("select name from sqlite_master")}
    finally:
        connection.close()
    assert {"records", "record_terms", "edges", "tombstones", "sources", "meta"} <= tables


@pytest.mark.parametrize("name", ["index.sqlite3", "writer.lock"])
def test_a_symlinked_index_or_lock_is_refused(root, tmp_path, name):
    directory = root / "knowledge"
    directory.mkdir(mode=0o700)
    (tmp_path / "target").write_text("")
    (directory / name).symlink_to(tmp_path / "target")
    with pytest.raises(BoundaryError, match="symlink"):
        apply(root, batch([rec("R1")]))
    assert (tmp_path / "target").read_text() == ""


def test_a_symlinked_knowledge_directory_is_refused(root, tmp_path):
    (tmp_path / "real").mkdir()
    (root / "knowledge").symlink_to(tmp_path / "real")
    with pytest.raises(BoundaryError, match="symlink"):
        apply(root, batch([rec("R1")]))
    assert list((tmp_path / "real").iterdir()) == []


def test_no_index_yet(root):
    assert store.exists(root) is False
    with pytest.raises(InputError, match="knowledge update"):
        with store.reader(root):
            pass
    assert not (root / "knowledge").exists()


# Generations and differences ---------------------------------------------------------------


def test_a_first_update_creates_generation_one(root):
    summary = apply(root, batch([rec("R1"), rec("R2")]))
    assert summary["generation"] == {"before": 0, "after": 1}
    assert summary["advanced"] is True
    assert summary["inserted"] == sorted(model.record_id("synthetic", k) for k in ("R1", "R2"))
    generation, seen = snapshot(root)
    assert generation == 1 and len(seen) == 2


def test_the_same_input_twice_does_not_advance(root):
    apply(root, batch([rec("R1")]))
    summary = apply(root, batch([model.normalize(item("R1"), LATER)]), LATER)
    assert summary["advanced"] is False
    assert summary["generation"] == {"before": 1, "after": 1}
    assert summary["unchanged"] == 1 and summary["inserted"] == summary["updated"] == []


def test_the_source_cursor_moves_even_when_nothing_else_does(root):
    apply(root, batch([rec("R1")], cursor="k1"))
    apply(root, batch([rec("R1")], cursor="k2"), LATER)
    with store.reader(root) as reader:
        assert reader.cursor("file:synthetic") == "k2"
        assert reader.cursor("file:other") is None
        (source,) = reader.sources()
    assert source == {
        "name": "file:synthetic",
        "adapter": "file",
        "cursor": "k2",
        "state": "complete",
        "last_success_at": LATER,
    }


def test_a_changed_record_advances_and_is_updated(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    summary = apply(root, batch([rec("R1", summary="変えた"), rec("R2")]))
    assert summary["advanced"] is True and summary["updated"] == [
        model.record_id("synthetic", "R1")
    ]
    with store.reader(root) as reader:
        (row,) = [r for r in reader.records(EVERYONE) if r["summary"] == "変えた"]
        assert reader.matching(EVERYONE, ["変え"])[0][0] == row["id"]


def test_a_non_normative_change_is_still_written(root):
    apply(root, batch([rec("R1")]))
    summary = apply(root, batch([rec("R1", facets={"topic": ["backup"]})]))
    assert summary["updated"] == [model.record_id("synthetic", "R1")]
    with store.reader(root) as reader:
        assert reader.records(EVERYONE)[0]["facets"]["topic"] == ["backup"]


def test_a_complete_update_tombstones_what_the_source_dropped(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    summary = apply(root, batch([rec("R1")]))
    dropped = model.record_id("synthetic", "R2")
    assert summary["advanced"] is True
    assert summary["tombstoned"] == [{"id": dropped, "reason": "removed_from_source"}]
    with store.reader(root) as reader:
        assert [row["id"] for row in reader.records(EVERYONE)] == [
            model.record_id("synthetic", "R1")
        ]
        (tombstone,) = reader.tombstones()
    assert tombstone["id"] == dropped and tombstone["generation"] == 2
    assert tombstone["origin"] == {"kind": "synthetic", "key": "R2"}


def test_a_partial_update_never_tombstones_what_it_did_not_see(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    summary = apply(root, batch([rec("R1")], complete=False))
    assert summary["tombstoned"] == [] and summary["advanced"] is False
    _, seen = snapshot(root)
    assert len(seen) == 2
    with store.reader(root) as reader:
        assert reader.sources()[0]["state"] == "partial"


def test_a_complete_update_leaves_other_sources_alone(root):
    apply(root, batch([rec("R1")], source="file:one"))
    apply(root, batch([rec("R2")], source="file:two"))
    _, seen = snapshot(root)
    assert len(seen) == 2


def test_an_explicit_tombstone_applies_even_to_a_partial_update(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    gone = model.record_id("synthetic", "R2")
    stone = {"id": gone, "origin": {"kind": "synthetic", "key": "R2"}, "reason": "expired"}
    summary = apply(root, batch([], complete=False, tombstones=[stone]))
    assert summary["tombstoned"] == [{"id": gone, "reason": "expired"}]
    _, seen = snapshot(root)
    assert gone not in seen


def test_a_rejected_record_is_taken_out_of_the_index(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    gone = model.record_id("synthetic", "R2")
    summary = apply(root, batch([rec("R1")], complete=False, rejected=[gone]))
    assert summary["tombstoned"] == [{"id": gone, "reason": "rejected"}]


def test_a_record_that_comes_back_loses_its_tombstone(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    apply(root, batch([rec("R1")]))
    apply(root, batch([rec("R1"), rec("R2")]))
    with store.reader(root) as reader:
        assert reader.tombstones() == []
        assert len(reader.records(EVERYONE)) == 2


def test_a_changed_config_advances_the_next_writer(root):
    apply(root, batch([rec("R1")], config_hash="c1"))
    summary = apply(root, batch([rec("R1")], config_hash="c2"))
    assert summary["advanced"] is True and summary["config_changed"] is True
    assert summary["generation"] == {"before": 1, "after": 2}
    again = apply(root, batch([rec("R1")], config_hash="c2"))
    assert again["advanced"] is False and again["config_changed"] is False


def test_a_supersession_cycle_writes_nothing(root):
    apply(root, batch([rec("R1")]))
    before = snapshot(root)
    cyclic = [
        rec("R1", supersedes=[{"kind": "synthetic", "key": "R2"}]),
        rec("R2", supersedes=[{"kind": "synthetic", "key": "R1"}]),
    ]
    with pytest.raises(InputError, match="cycle") as stopped:
        apply(root, batch(cyclic))
    assert stopped.value.exit_code == 2
    assert snapshot(root) == before


def test_a_cycle_through_a_record_already_indexed_is_found(root):
    apply(
        root, batch([rec("R1", supersedes=[{"kind": "synthetic", "key": "R2"}])], source="file:one")
    )
    with pytest.raises(InputError, match="cycle"):
        apply(
            root,
            batch([rec("R2", supersedes=[{"kind": "synthetic", "key": "R1"}])], source="file:two"),
        )


def test_supersedes_may_name_a_record_not_indexed_yet(root):
    summary = apply(root, batch([rec("R3", supersedes=[{"kind": "synthetic", "key": "R9"}])]))
    assert summary["advanced"] is True


def test_the_plan_is_what_the_writer_would_do_and_writes_nothing(root):
    apply(root, batch([rec("R1"), rec("R2")]))
    database = (root / "knowledge" / "index.sqlite3").read_bytes()
    proposed = batch([rec("R1", summary="変えた"), rec("R3")])
    with store.reader(root) as reader:
        plan = reader.plan(proposed)
    assert (root / "knowledge" / "index.sqlite3").read_bytes() == database
    assert snapshot(root)[0] == 1
    assert apply(root, proposed) == plan


def test_the_plan_also_refuses_a_cycle(root):
    apply(root, batch([rec("R1", supersedes=[{"kind": "synthetic", "key": "R2"}])]))
    with store.reader(root) as reader:
        with pytest.raises(InputError, match="cycle"):
            reader.plan(
                batch(
                    [rec("R2", supersedes=[{"kind": "synthetic", "key": "R1"}])], source="file:two"
                )
            )


def test_status_counts_records_by_status(root):
    apply(
        root,
        batch(
            [rec("R1"), rec("R2", status="retired"), rec("R3", status="proposed", approval=None)]
        ),
    )
    with store.reader(root) as reader:
        assert reader.status_counts() == {"approved": 1, "proposed": 1, "retired": 1}
        meta = reader.meta()
    assert meta["generation"] == 1 and meta["schema_version"] == store.SCHEMA_VERSION
    assert meta["updated_at"] == NOW


def test_an_index_from_another_schema_version_is_refused(root):
    apply(root, batch([rec("R1")]))
    connection = sqlite3.connect(root / "knowledge" / "index.sqlite3")
    with connection:
        connection.execute("update meta set value = '99' where key = 'schema_version'")
    connection.close()
    with pytest.raises(InputError, match="schema version"):
        with store.reader(root):
            pass
    with pytest.raises(InputError, match="schema version"):
        apply(root, batch([rec("R1")]))


# Access -----------------------------------------------------------------------------------


def test_the_access_filter_needs_both_a_classification_and_a_shared_reader(root):
    apply(
        root,
        batch(
            [
                rec("shared", access={"classification": "internal", "readers": ["team-alpha"]}),
                rec("gamma", access={"classification": "internal", "readers": ["team-gamma"]}),
                rec("secret", access={"classification": "confidential", "readers": ["team-alpha"]}),
                rec(
                    "both",
                    access={"classification": "public", "readers": ["team-gamma", "everyone"]},
                ),
            ]
        ),
    )
    with store.reader(root) as reader:
        seen_b = {row["origin"]["key"] for row in reader.records(LIMITED)}
        seen_a = {row["origin"]["key"] for row in reader.records(EVERYONE)}
        hits = {row_id for row_id, _ in reader.matching(LIMITED, ["保持"])}
    assert hits == {model.record_id("synthetic", key) for key in ("shared", "both")}
    assert seen_b == {"shared", "both"}
    assert seen_a == {"shared", "gamma", "secret", "both"}


def test_a_principal_with_nothing_in_common_reads_nothing(root):
    apply(root, batch([rec("R1")]))
    nobody = config.Principal("nobody", frozenset({"team-zeta"}), frozenset({"public"}))
    with store.reader(root) as reader:
        assert reader.records(nobody) == []
        assert reader.matching(nobody, ["保持"]) == []


def test_records_can_be_asked_for_by_id_and_still_pass_the_filter(root):
    apply(
        root,
        batch(
            [rec("R1"), rec("G", access={"classification": "internal", "readers": ["team-gamma"]})]
        ),
    )
    wanted = [model.record_id("synthetic", "R1"), model.record_id("synthetic", "G")]
    with store.reader(root) as reader:
        assert [row["id"] for row in reader.records(LIMITED, ids=wanted)] == [wanted[0]]
        assert reader.records(LIMITED, ids=[]) == []


def test_the_access_clause_is_sql_and_bound_parameters():
    clause, params = store.access_clause(LIMITED)
    assert "?" in clause and "team-alpha" not in clause
    assert sorted(params) == sorted(["everyone", "team-alpha", "public", "synthetic", "internal"])


# Locking and transactions -------------------------------------------------------------------


def test_a_second_writer_exits_1_and_writes_nothing(root):
    apply(root, batch([rec("R1")]))
    before = snapshot(root)
    with store.writer(root):
        with pytest.raises(QualityGateError, match="writer") as busy:
            apply(root, batch([rec("R2")]))
        assert busy.value.exit_code == 1
    assert snapshot(root) == before


def test_a_second_writer_from_another_thread_is_refused_too(root):
    apply(root, batch([rec("R1")]))
    outcome = {}

    def other():
        try:
            apply(root, batch([rec("R2")]))
            outcome["result"] = "wrote"
        except QualityGateError:
            outcome["result"] = "refused"

    with store.writer(root):
        thread = threading.Thread(target=other)
        thread.start()
        thread.join(10)
    assert outcome == {"result": "refused"}
    assert len(snapshot(root)[1]) == 1


def test_an_error_inside_the_transaction_changes_nothing(root, monkeypatch):
    apply(root, batch([rec("R1"), rec("R2")]))
    before = snapshot(root)
    calls = []
    original = store._index_terms

    def failing(connection, record):
        calls.append(record["id"])
        if len(calls) == 2:
            raise RuntimeError("disk went away")
        original(connection, record)

    monkeypatch.setattr(store, "_index_terms", failing)
    with pytest.raises(RuntimeError):
        apply(root, batch([rec("R1", summary="一"), rec("R2", summary="二"), rec("R3")]))
    monkeypatch.undo()
    assert len(calls) == 2
    assert snapshot(root) == before
    with store.reader(root) as reader:
        assert reader.cursor("file:synthetic") == "k1"
        assert reader.matching(EVERYONE, ["保持"])
    assert apply(root, batch([rec("R1"), rec("R2")]))["advanced"] is False


def test_a_reader_sees_the_last_generation_while_a_writer_is_inside_its_transaction(
    root, monkeypatch
):
    apply(root, batch([rec("R1")]))
    before = snapshot(root)
    inside, release = threading.Event(), threading.Event()
    original = store._index_terms

    def slow(connection, record):
        original(connection, record)
        inside.set()
        assert release.wait(10)

    monkeypatch.setattr(store, "_index_terms", slow)
    errors = []

    def write():
        try:
            apply(root, batch([rec("R1", summary="変えた"), rec("R2")]))
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    thread = threading.Thread(target=write)
    thread.start()
    try:
        assert inside.wait(10)
        with store.reader(root) as reader:
            first = (
                reader.generation,
                {r["id"]: r["content_hash"] for r in reader.records(EVERYONE)},
            )
            assert first == before
            assert reader.matching(EVERYONE, ["変え"]) == []
    finally:
        release.set()
        thread.join(10)
    assert errors == []
    after = snapshot(root)
    assert after[0] == 2 and len(after[1]) == 2


def test_a_reader_keeps_its_snapshot_across_a_commit(root):
    apply(root, batch([rec("R1")]))
    with store.reader(root) as reader:
        generation = reader.generation
        apply(root, batch([rec("R1"), rec("R2")]))
        assert reader.generation == generation == 1
        assert len(reader.records(EVERYONE)) == 1
        assert reader.meta()["generation"] == 1
    assert snapshot(root)[0] == 2


def test_readers_never_see_a_mix_of_two_generations(root):
    """Several readers and one writer at once: each observation is one generation's state."""
    expected = {}
    apply(root, batch([rec("R0")]))
    expected[1] = snapshot(root)[1]
    stop = threading.Event()
    observed, errors = [], []

    def read():
        try:
            while not stop.is_set():
                with store.reader(root) as reader:
                    seen = {r["id"]: r["content_hash"] for r in reader.records(EVERYONE)}
                    observed.append((reader.generation, seen, reader.meta()["generation"]))
        except BaseException as error:  # noqa: BLE001 - reported below
            errors.append(error)

    readers = [threading.Thread(target=read) for _ in range(4)]
    for thread in readers:
        thread.start()
    try:
        for step in range(1, 13):
            records = [rec(f"R{n}", summary=f"R{n} 版 {step}") for n in range(step % 5 + 1)]
            summary = apply(root, batch(records))
            expected[summary["generation"]["after"]] = snapshot(root)[1]
    finally:
        stop.set()
        for thread in readers:
            thread.join(10)
    assert errors == []
    assert len(observed) > 12
    for generation, seen, meta_generation in observed:
        assert generation == meta_generation
        assert seen == expected[generation]


def test_provenance_names_the_source_of_readable_records_only(root):
    gamma = {"classification": "internal", "readers": ["team-gamma"]}
    apply(root, batch([rec("R1"), rec("G", access=gamma)]))
    with store.reader(root) as reader:
        assert reader.provenance(LIMITED) == {model.record_id("synthetic", "R1"): "file:synthetic"}
        assert set(reader.provenance(EVERYONE)) == {
            model.record_id("synthetic", "R1"),
            model.record_id("synthetic", "G"),
        }


def test_edges_are_returned_only_between_readable_records(root):
    gamma = {"classification": "internal", "readers": ["team-gamma"]}
    apply(
        root,
        batch(
            [
                rec("R1"),
                rec("R2", supersedes=[{"kind": "synthetic", "key": "R1"}]),
                rec("G", access=gamma, supersedes=[{"kind": "synthetic", "key": "R2"}]),
            ]
        ),
    )
    r1, r2, g = (model.record_id("synthetic", key) for key in ("R1", "R2", "G"))
    path = store.directory(root) / store.INDEX
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.executemany(
        "INSERT INTO edges (source_id, target_id, relation) VALUES (?, ?, 'conflicts_with')",
        [(r1, r2), (r1, g)],
    )
    connection.close()
    with store.reader(root) as reader:
        assert reader.edges(LIMITED, "supersedes") == [(r2, r1)]
        assert reader.edges(LIMITED, "conflicts_with") == [(r1, r2)]
        assert sorted(reader.edges(EVERYONE, "supersedes")) == sorted([(r2, r1), (g, r2)])
        assert sorted(reader.edges(EVERYONE, "conflicts_with")) == sorted([(r1, r2), (r1, g)])
