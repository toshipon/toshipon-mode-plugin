"""The `review-registry` source adapter: registry entries as knowledge items, read only."""

import json

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import paths, records
from sre_harness.errors import InputError
from sre_harness.knowledge import config, ingest, model, store
from sre_harness.knowledge.adapters import ADAPTERS, build
from sre_harness.knowledge.adapters import review_registry as adapter_module
from sre_harness.review import approve, registry

NOW = "2026-11-27T00:00:00Z"
ORIGIN = "review-registry"


@pytest.fixture
def root(tmp_path):
    state = paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "state")})
    paths.write_private(state / "knowledge" / "config.json", records.canonical(rr.CONFIG))
    return state


@pytest.fixture
def made(root):
    return rr.write(root)


def changes(root):
    return build("review-registry", {"root": root}).changes(None)


def by_key(items):
    return {item["origin"]["key"]: item for item in items}


def files(root):
    """(bytes, mtime) of every file under registry/."""
    base = root / "registry"
    return {
        str(path.relative_to(base)): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in sorted(base.rglob("*"))
        if path.is_file()
    }


def test_the_adapter_is_registered_under_its_source_name():
    assert "review-registry" in ADAPTERS


def test_every_mapped_valid_entry_becomes_one_item_at_its_latest_version(root, made):
    found = changes(root)
    items = by_key(found.items)
    assert sorted(items) == ["RG-9001", "RG-9002", "RG-9003", "RG-9004", "RG-9005"]
    assert found.complete is True and found.tombstones == []
    assert items["RG-9005"]["origin"] == {"kind": ORIGIN, "key": "RG-9005", "version": 2}
    assert {key: item["status"] for key, item in items.items()} == {
        "RG-9001": "approved",
        "RG-9002": "approved",
        "RG-9003": "retired",
        "RG-9004": "rejected",
        "RG-9005": "approved",
    }


def test_the_form_decides_the_kind(root, made):
    items = by_key(changes(root).items)
    assert items["RG-9001"]["kind"] == "rule"
    assert items["RG-9002"]["kind"] == "policy_check"
    assert items["RG-9005"]["kind"] == "policy_check"


def test_an_entry_without_a_knowledge_block_is_project_scoped_default_access_advisory(root, made):
    items = by_key(changes(root).items)
    for key, item in items.items():
        project = "project-gamma" if key == "RG-9005" else "project-alpha"
        assert item["scope"] == {
            "level": "project",
            "project_id": project,
            "component_id": None,
            "task_id": None,
        }
        assert item["access"] == rr.CONFIG["projects"][project]["default_access"]
        assert item["enforcement"] == "advisory"
        assert item["applies_when"] == [] and item["exceptions"] == []


def test_summary_and_path_globs_come_from_the_artifact(root, made):
    items = by_key(changes(root).items)
    assert items["RG-9001"]["summary"] == "外向きの HTTP 呼び出しには timeout を設定する"
    assert items["RG-9005"]["summary"] == "HTTP の timeout を検査する（改訂）"
    assert items["RG-9002"]["facets"]["path_globs"] == ["infra/queue/**"]
    assert items["RG-9001"]["facets"]["path_globs"] == []
    assert items["RG-9002"]["facets"]["repo"] == rr.ALPHA


def test_evidence_points_at_the_latest_artifact_with_its_hash_and_size(root, made):
    items = by_key(changes(root).items)
    (evidence,) = items["RG-9005"]["evidence"]
    text = (root / "registry" / "RG-9005" / "v0002.md").read_text(encoding="utf-8")
    assert evidence == {
        "locator": "registry:RG-9005/v0002.md",
        "revision": "v0002",
        "content_sha256": records.sha256(text),
        "size": len(text.encode("utf-8")),
    }


def test_the_approval_record_is_copied_from_the_latest_approved_version(root, made):
    items = by_key(changes(root).items)
    assert items["RG-9001"]["approval"] == {
        "approver": "alice",
        "decided_at": "2026-10-01T00:00:00Z",
        "via": "terminal_hash_prefix",
    }
    assert items["RG-9005"]["approval"]["decided_at"] == "2026-10-02T00:00:00Z"
    assert items["RG-9003"]["approval"]["decided_at"] == "2026-10-01T00:00:00Z"  # retired at v2
    assert items["RG-9004"]["approval"] is None  # never approved
    assert items["RG-9003"]["updated_at"] == "2026-10-02T00:00:00Z"


def test_every_item_passes_the_update_checks(root, made):
    accepted, rejected = ingest.check(changes(root).items, config.known_readers(rr.CONFIG), NOW)
    assert rejected == [] and len(accepted) == 5
    assert all(record["enforcement"] == "advisory" for record in accepted)


def test_unmapped_and_invalid_entries_are_listed_and_not_items(root, made):
    found = changes(root)
    skipped = {entry["entry"]: entry for entry in found.skipped}
    assert sorted(skipped) == sorted(made.unmapped + made.invalid)
    assert skipped["RG-9006"]["reason"] == "repo_not_in_config"
    assert skipped["RG-9006"]["kind"] == "unmapped" and skipped["RG-9006"]["repo"] == rr.STRAY
    assert skipped["RG-9007"]["reason"] == "hash_mismatch"
    assert skipped["RG-9007"]["kind"] == "invalid"
    assert skipped["RG-9007"]["id"] == model.record_id(ORIGIN, "RG-9007")
    assert not {"RG-9006", "RG-9007"} & set(by_key(found.items))


def test_a_repo_in_two_projects_is_unmapped(root, made):
    settings = json.loads(json.dumps(rr.CONFIG))
    settings["projects"]["project-gamma"]["repos"].append(rr.ALPHA)
    paths.write_private(root / "knowledge" / "config.json", records.canonical(settings))
    skipped = {entry["entry"]: entry["reason"] for entry in changes(root).skipped}
    assert skipped["RG-9001"] == "repo_in_several_projects"


def test_an_unreadable_artifact_is_invalid(root, made):
    (root / "registry" / "RG-9001" / "v0001.md").unlink()
    skipped = {entry["entry"]: entry["reason"] for entry in changes(root).skipped}
    assert skipped["RG-9001"] == "artifact_unreadable"


def test_a_record_that_disagrees_with_its_artifact_is_invalid(root, made):
    artifact = rr.text("題名", "本文。")
    record = rr.version("RG-9100", 1, "approved", rr.ALPHA, artifact)
    record["candidate"]["title"] = "別の題名"
    registry.write_version(root, record, artifact)
    skipped = {entry["entry"]: entry["reason"] for entry in changes(root).skipped}
    assert skipped["RG-9100"] == "content_mismatch"


def test_a_long_reviewer_name_is_cut_to_what_the_record_holds(root, made):
    artifact = rr.text("長い名前の承認者", "本文。")
    record = rr.version("RG-9100", 1, "approved", rr.ALPHA, artifact)
    record["decision"]["reviewer"] = "a" * 200
    registry.write_version(root, record, artifact)
    item = by_key(changes(root).items)["RG-9100"]
    assert item["approval"]["approver"] == "a" * adapter_module.APPROVER_LENGTH


def test_the_registry_is_only_read(root, made):
    before = files(root)
    changes(root)
    ingest.update(root, build("review-registry", {"root": root}), now=NOW)
    assert files(root) == before


def test_the_cursor_follows_the_latest_version_of_each_entry(root, made):
    first = changes(root).cursor
    assert first == changes(root).cursor and first.startswith("sha256:")
    approve.retire(root, "RG-9001", "alice", "合成データの取り下げ")
    assert changes(root).cursor != first


def test_the_adapter_takes_no_input_file(root):
    with pytest.raises(InputError, match="--input"):
        build("review-registry", {"root": root, "input": "x.json"})


def test_no_registry_is_an_empty_complete_source(root):
    found = changes(root)
    assert found.items == [] and found.skipped == [] and found.complete is True


def test_an_update_indexes_the_items_and_a_new_version_is_picked_up(root, made):
    source = build("review-registry", {"root": root})
    first = ingest.update(root, source, now=NOW)
    assert first["generation"] == {"before": 0, "after": 1}
    assert first["counts"]["indexed"] == 5 and first["counts"]["rejected"] == 0
    assert sorted(entry["entry"] for entry in first["skipped"]) == ["RG-9006", "RG-9007"]
    again = ingest.update(root, build("review-registry", {"root": root}), now=NOW)
    assert again["generation"] == {"before": 1, "after": 1} and again["advanced"] is False
    approve.retire(root, "RG-9001", "alice", "合成データの取り下げ")
    moved = ingest.update(root, build("review-registry", {"root": root}), now=NOW)
    assert moved["generation"] == {"before": 1, "after": 2}
    assert moved["counts"]["updated"] == 1
    principal = config.principal(rr.CONFIG, "workspace-a")
    with store.reader(root) as reader:
        (row,) = reader.records(principal, ids=[model.record_id(ORIGIN, "RG-9001")])
    assert row["status"] == "retired" and row["origin"]["version"] == 2
