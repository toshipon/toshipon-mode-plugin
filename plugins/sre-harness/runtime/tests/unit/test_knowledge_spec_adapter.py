"""The `spec-run` source adapter: one run's Decision and Requirement Registries as knowledge
items keyed by the ids documents write. Read only, never mandatory. Everything is synthetic."""

import json

import pytest

from sre_harness import paths, records
from sre_harness.errors import InputError
from sre_harness.knowledge import config, model, store
from sre_harness.knowledge.adapters import ADAPTERS, build
from sre_harness.spec import trace
from tests.knowledge_helpers import (
    SPEC_CREATED_AT,
    SPEC_RUN_ID,
    section_text,
    spec_decision,
    spec_link,
    spec_requirement,
    state_root,
    update_spec,
    write_config,
    write_spec_run,
)
from tests.knowledge_helpers import update as update_file

OTHER_RUN = "run-20261126T000000Z-0000cafe"


@pytest.fixture
def env(home):
    write_config(home)
    return home


def changes(env, run_id=SPEC_RUN_ID, project="project-alpha"):
    adapter = build("spec-run", {"root": state_root(env), "run": run_id, "project": project})
    return adapter, adapter.changes(None)


def by_key(items):
    return {item["origin"]["key"]: item for item in items}


def rid(kind, written, project="project-alpha"):
    return model.record_id(kind, f"{project}:{written}")


def standard(env, run_id=SPEC_RUN_ID, links=None):
    """ADR-0001 <- ADR-0002 (approved chain), ADR-0003 rejected, ADR-0004 proposed, one
    decision without a document id, one inferred decision; REQ-0001 explicit, RQI-0002."""
    drafts = [
        spec_decision(1, status="approved"),
        spec_decision(2, status="approved", supersedes=["ADR-0001"]),
        spec_decision(3, status="rejected"),
        spec_decision(4, status="proposed"),
        spec_decision(5, status="proposed"),
        spec_decision(6, status="proposed", basis="inferred"),
    ]
    drafts[4].pop("external_id")
    requirements = [
        spec_requirement(1, decision_refs=["DEC-0002"]),
        spec_requirement(2, basis="inferred"),
    ]
    return write_spec_run(env, drafts, requirements, links, run_id=run_id)


# The items -------------------------------------------------------------------------------------


def test_the_adapter_is_registered_under_its_source_name():
    assert "spec-run" in ADAPTERS


def test_decisions_are_keyed_by_the_document_id_of_the_project(env):
    standard(env)
    adapter, found = changes(env)
    assert adapter.source == "spec-run:project-alpha"
    assert found.complete is True and found.tombstones == []
    items = by_key(found.items)
    assert sorted(items) == [
        "project-alpha:ADR-0001",
        "project-alpha:ADR-0002",
        "project-alpha:ADR-0003",
        "project-alpha:ADR-0004",
        "project-alpha:REQ-0001",
    ]
    first = items["project-alpha:ADR-0001"]
    assert first["origin"] == {"kind": "decision", "key": "project-alpha:ADR-0001"}
    assert first["kind"] == "decision" and first["facets"] == {"decision_id": "ADR-0001"}
    assert first["status"] == "superseded"
    assert first["scope"] == {
        "level": "project",
        "project_id": "project-alpha",
        "component_id": None,
        "task_id": None,
    }
    assert first["access"] == {"classification": "internal", "readers": ["team-alpha"]}
    assert first["confidence"] == {"value": "high", "vocabulary": "spec"}
    assert first["updated_at"] == SPEC_CREATED_AT
    assert {key: item["status"] for key, item in items.items()} == {
        "project-alpha:ADR-0001": "superseded",
        "project-alpha:ADR-0002": "approved",
        "project-alpha:ADR-0003": "rejected",
        "project-alpha:ADR-0004": "proposed",
        "project-alpha:REQ-0001": "proposed",
    }


def test_decisions_without_a_document_id_and_inferred_entries_are_skipped(env):
    standard(env)
    _, found = changes(env)
    assert found.skipped == [
        {
            "entry": "DEC-0005",
            "id": None,
            "kind": "decision",
            "status": "proposed",
            "reason": "no_external_id",
        },
        {
            "entry": "DCI-0006",
            "id": None,
            "kind": "decision",
            "status": "proposed",
            "reason": "inferred",
        },
        {
            "entry": "RQI-0002",
            "id": None,
            "kind": "requirement",
            "status": None,
            "reason": "inferred",
        },
    ]


def test_approval_is_copied_from_the_registry_for_approved_and_superseded_only(env):
    standard(env)
    items = by_key(changes(env)[1].items)
    approval = {"approver": "alice", "decided_at": "2026-01-10T00:00:00Z", "via": "spec-document"}
    assert items["project-alpha:ADR-0001"]["approval"] == approval
    assert items["project-alpha:ADR-0002"]["approval"] == approval
    assert items["project-alpha:ADR-0003"]["approval"] is None
    assert items["project-alpha:REQ-0001"]["approval"] is None


def test_a_decision_without_a_date_is_approved_at_the_run_time(env):
    write_spec_run(env, [spec_decision(1, decided_at=None)])
    (item,) = changes(env)[1].items
    assert item["approval"]["decided_at"] == SPEC_CREATED_AT


def test_supersession_is_mapped_onto_the_records(env):
    standard(env)
    items = by_key(changes(env)[1].items)
    assert items["project-alpha:ADR-0002"]["supersedes"] == [
        {"kind": "decision", "key": "project-alpha:ADR-0001"}
    ]
    assert items["project-alpha:ADR-0001"]["supersedes"] == []


def test_evidence_points_at_the_run_sections_with_their_hash(env):
    standard(env)
    item = by_key(changes(env)[1].items)["project-alpha:ADR-0002"]
    text = section_text("DOC-0002")
    assert item["evidence"] == [
        {
            "locator": f"run:{SPEC_RUN_ID}/documents.jsonl#DOC-0002#S1",
            "revision": SPEC_RUN_ID,
            "content_sha256": records.sha256(text),
            "size": len(text.encode("utf-8")),
        }
    ]


def test_an_entry_whose_section_is_missing_is_skipped(env):
    standard(env)
    path = paths.child(state_root(env), "runs", SPEC_RUN_ID, "documents.jsonl")
    rows = [row for row in records.read_jsonl(path) if row["id"] != "DOC-0003"]
    records.write_jsonl(path, rows)
    _, found = changes(env)
    assert "project-alpha:ADR-0003" not in by_key(found.items)
    assert {
        "entry": "DEC-0003",
        "id": rid("decision", "ADR-0003"),
        "kind": "decision",
        "status": "rejected",
        "reason": "evidence_unresolved",
    } in found.skipped


def test_no_relation_or_supersession_points_at_an_entry_left_out(env):
    links = [
        spec_link(1, ("decision", "DEC-0002"), ("decision", "DEC-0001"), "depends_on"),
        spec_link(2, ("requirement", "REQ-0001"), ("decision", "DEC-0001"), "refines"),
        spec_link(3, ("requirement", "REQ-0001"), ("decision", "DEC-0002"), "refines"),
    ]
    standard(env, links=links)
    path = paths.child(state_root(env), "runs", SPEC_RUN_ID, "documents.jsonl")
    records.write_jsonl(path, [row for row in records.read_jsonl(path) if row["id"] != "DOC-0001"])
    _, found = changes(env)
    items = by_key(found.items)
    assert "project-alpha:ADR-0001" not in items  # evidence_unresolved
    left_out = {"kind": "decision", "key": "project-alpha:ADR-0001"}
    assert items["project-alpha:ADR-0002"]["supersedes"] == []
    assert "relations" not in items["project-alpha:ADR-0002"]
    assert items["project-alpha:REQ-0001"]["relations"] == [
        {"relation": "refines", "target": {"kind": "decision", "key": "project-alpha:ADR-0002"}}
    ]
    for item in found.items:
        assert left_out not in [row["target"] for row in item.get("relations", [])]
        assert left_out not in item.get("supersedes", [])


@pytest.mark.parametrize("status", ["approved", "superseded", "rejected", "proposed", "unresolved"])
def test_every_spec_item_is_advisory_whatever_the_registry_says(env, status):
    # A document that says "approved", or anything else, never makes a rule mandatory here.
    drafts = [spec_decision(1), spec_decision(2, supersedes=["ADR-0001"])]
    drafts.append(spec_decision(3, status=status if status != "superseded" else "approved"))
    drafts.append(spec_decision(4, status="unresolved", content="enforcement: mandatory"))
    write_spec_run(env, drafts, [spec_requirement(1)])
    found = changes(env)[1].items
    assert found and {item["enforcement"] for item in found} == {"advisory"}


# Relations from trace.json ------------------------------------------------------------------------


def test_explicit_links_between_two_records_become_relations(env):
    links = [
        spec_link(1, ("decision", "DEC-0002"), ("decision", "DEC-0004"), "conflicts_with"),
        spec_link(2, ("requirement", "REQ-0001"), ("decision", "DEC-0002"), "refines"),
        spec_link(
            3, ("decision", "DEC-0003"), ("decision", "DEC-0004"), "conflicts_with", "inferred"
        ),
        spec_link(4, ("iac", "aws_sqs_queue.main"), ("decision", "DEC-0002"), "implements"),
        spec_link(5, ("decision", "DEC-0002"), ("decision", "DEC-0005"), "depends_on"),
        spec_link(6, ("decision", "ADR-0003"), ("decision", "DEC-0002"), "depends_on"),
    ]
    standard(env, links=links)
    _, found = changes(env)
    items = by_key(found.items)
    assert items["project-alpha:ADR-0002"]["relations"] == [
        {
            "relation": "conflicts_with",
            "target": {"kind": "decision", "key": "project-alpha:ADR-0004"},
        }
    ]
    assert items["project-alpha:REQ-0001"]["relations"] == [
        {"relation": "refines", "target": {"kind": "decision", "key": "project-alpha:ADR-0002"}}
    ]
    assert items["project-alpha:ADR-0003"]["relations"] == [
        {"relation": "depends_on", "target": {"kind": "decision", "key": "project-alpha:ADR-0002"}}
    ]
    assert "relations" not in items["project-alpha:ADR-0004"]
    assert {
        "entry": "TL-0003",
        "id": None,
        "kind": "relation",
        "status": None,
        "reason": "inferred",
    } in found.skipped


def test_relation_names_are_the_trace_vocabulary():
    names = set(trace.RELATION_NAMES)
    assert names == set(records.schema("trace-link")["properties"]["relation"]["enum"])
    kinds = records.schema("knowledge-record")["$defs"]["relation"]["properties"]["relation"]
    assert names == set(kinds["enum"])


# Refusals ---------------------------------------------------------------------------------------


def test_a_project_not_in_the_config_is_refused(env):
    standard(env)
    with pytest.raises(InputError, match="project-nowhere") as refused:
        changes(env, project="project-nowhere")
    assert refused.value.exit_code == 2


def test_the_source_needs_the_config(home):
    standard(home)
    with pytest.raises(InputError, match="config.json"):
        changes(home)


@pytest.mark.parametrize(
    "options, message",
    [
        ({"run": SPEC_RUN_ID}, "--project"),
        ({"project": "project-alpha"}, "--run"),
        ({"run": SPEC_RUN_ID, "project": "project-alpha", "input": "x.json"}, "no --input"),
    ],
)
def test_the_options_are_checked(env, options, message):
    with pytest.raises(InputError, match=message):
        build("spec-run", {"root": state_root(env), **options})


@pytest.mark.parametrize("source", ["file", "review-registry"])
def test_the_other_sources_take_no_run_or_project(env, source):
    with pytest.raises(InputError, match="--run"):
        build(source, {"root": state_root(env), "input": "x.json", "run": SPEC_RUN_ID})


def test_a_run_that_does_not_exist_is_refused(env):
    with pytest.raises(InputError, match="does not exist"):
        changes(env, run_id=OTHER_RUN)


def built(drafts):
    from sre_harness.spec import registry as spec_registry

    return spec_registry.build([{**draft, "run_id": SPEC_RUN_ID} for draft in drafts])


def test_a_registry_that_breaks_its_invariants_is_refused_and_nothing_is_written(env):
    standard(env)
    assert update_spec(env)[0] == 0
    with store.reader(state_root(env)) as reader:
        before = reader.generation
    decisions = built([spec_decision(1), spec_decision(2, supersedes=["ADR-0001"])])
    decisions["decisions"][1]["status"] = "rejected"  # still named as ADR-0001's successor
    write_spec_run(env, [], built=decisions)
    code, _ = update_spec(env)
    assert code == 2
    with store.reader(state_root(env)) as reader:
        assert reader.generation == before


def test_a_supersession_cycle_is_refused_with_exit_2(env):
    decisions = built([spec_decision(1), spec_decision(2, supersedes=["ADR-0001"])])
    first, second = decisions["decisions"]
    first.update(supersedes=["DEC-0002"], superseded_by=[], status="approved")
    second.update(supersedes=["DEC-0001"], superseded_by=[], status="approved")
    write_spec_run(env, [], built=decisions)
    with pytest.raises(InputError, match="cycle"):
        changes(env)
    code, _ = update_spec(env)
    assert code == 2 and not store.exists(state_root(env))


def test_a_registry_file_of_another_run_is_refused(env):
    standard(env)
    path = paths.child(state_root(env), "runs", SPEC_RUN_ID, "decisions.json")
    value = json.loads(path.read_text())
    value["run_id"] = OTHER_RUN
    records.write_json(path, value)
    with pytest.raises(InputError, match="names run"):
        changes(env)


def test_an_invalid_registry_file_is_refused(env):
    standard(env)
    path = paths.child(state_root(env), "runs", SPEC_RUN_ID, "requirements.json")
    records.write_json(path, {"schema_version": 1})
    with pytest.raises(InputError, match="requirements.json of the run is invalid"):
        changes(env)


def test_a_run_without_its_registries_is_refused(env):
    paths.child(state_root(env), "runs", OTHER_RUN).mkdir(parents=True)
    with pytest.raises(InputError, match="spec extract"):
        changes(env, run_id=OTHER_RUN)


# Through the index --------------------------------------------------------------------------------


def test_the_update_reports_what_it_skipped_and_the_cursor_names_the_run(env):
    standard(env)
    code, result = update_spec(env)
    assert code == 0, result
    assert result["source"] == "spec-run:project-alpha" and result["complete"] is True
    assert result["cursor"].startswith(f"{SPEC_RUN_ID}:sha256:")
    assert result["counts"]["indexed"] == 5 and result["counts"]["rejected"] == 0
    assert [row["reason"] for row in result["skipped"]] == [
        "no_external_id",
        "inferred",
        "inferred",
    ]


def test_another_run_with_the_same_document_ids_updates_the_same_records(env):
    standard(env)
    assert update_spec(env)[0] == 0
    first = {row["id"] for row in records_of(env)}
    drafts = [spec_decision(1), spec_decision(2, supersedes=["ADR-0001"]), spec_decision(7)]
    write_spec_run(env, drafts, [spec_requirement(1)], run_id=OTHER_RUN)
    code, result = update_spec(env, OTHER_RUN)
    assert code == 0, result
    rows = {row["origin"]["key"]: row for row in records_of(env)}
    assert rid("decision", "ADR-0001") in first
    assert rows["project-alpha:ADR-0001"]["id"] == rid("decision", "ADR-0001")
    assert rows["project-alpha:ADR-0002"]["evidence"][0]["revision"] == OTHER_RUN
    assert "project-alpha:ADR-0007" in rows
    # The new run is the registry's latest state: what only the old run held is gone.
    gone = {row["id"]: row["reason"] for row in result["tombstoned"]}
    assert gone == {
        rid("decision", "ADR-0003"): "removed_from_source",
        rid("decision", "ADR-0004"): "removed_from_source",
    }
    assert result["counts"]["updated"] == 3 and result["counts"]["inserted"] == 1


def test_one_project_update_leaves_the_other_sources_alone(env, tmp_path):
    from tests.knowledge_helpers import item, write_input

    assert update_file(env, write_input(tmp_path / "in.json", [item("SYN-1")]))[0] == 0
    standard(env)
    assert update_spec(env)[0] == 0
    write_spec_run(env, [spec_decision(9)], [], run_id=OTHER_RUN)
    assert update_spec(env, OTHER_RUN, "project-gamma")[0] == 0
    keys = {row["origin"]["key"] for row in records_of(env)}
    assert "SYN-1" in keys and "project-alpha:ADR-0002" in keys and "project-gamma:ADR-0009" in keys


def test_a_dry_run_writes_nothing(env):
    standard(env)
    code, result = update_spec(env, SPEC_RUN_ID, "project-alpha", "--dry-run")
    assert code == 0 and result["dry_run"] is True and result["counts"]["inserted"] == 5
    assert not store.exists(state_root(env))


def records_of(env):
    everyone = config.Principal(
        "test",
        frozenset({"everyone", "team-alpha", "team-gamma"}),
        frozenset(config.CLASSIFICATIONS),
    )
    with store.reader(state_root(env)) as reader:
        return reader.records(everyone)


def test_relations_are_stored_as_edges_and_leave_with_their_record(env):
    links = [spec_link(1, ("decision", "DEC-0002"), ("decision", "DEC-0004"), "conflicts_with")]
    standard(env, links=links)
    assert update_spec(env)[0] == 0
    everyone = config.Principal("test", frozenset({"team-alpha"}), frozenset({"internal"}))
    with store.reader(state_root(env)) as reader:
        edges = reader.edges(everyone, store.CONFLICTS_WITH)
    assert edges == [(rid("decision", "ADR-0002"), rid("decision", "ADR-0004"))]
    standard(env, links=[])
    assert update_spec(env)[0] == 0
    with store.reader(state_root(env)) as reader:
        assert reader.edges(everyone, store.CONFLICTS_WITH) == []
