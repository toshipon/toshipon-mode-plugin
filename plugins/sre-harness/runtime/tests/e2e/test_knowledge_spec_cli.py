"""`knowledge update --source spec-run` end to end: the synthetic spec workspace through
`spec ingest` and `spec extract` (stub), then into the index, then search and fetch, all through
`cli.main`. Everything is synthetic."""

import json

import pytest

from examples.synthetic import spec_scenarios as sc
from sre_harness import paths, records
from sre_harness.knowledge import model
from tests.helpers import run_cli
from tests.knowledge_helpers import (
    SPEC_CREATED_AT,
    fetch,
    state_root,
    update_spec,
    write_config,
)
from tests.spec_helpers import extract, ingested, read

PROJECT = "project-alpha"


def rid(written, project=PROJECT, kind="decision"):
    return model.record_id(kind, f"{project}:{written}")


def extracted(env, manifest):
    run_id = ingested(env, manifest)
    code, result = extract(env, run_id)
    assert code == 0, result
    return run_id


@pytest.fixture
def workspace(tmp_path, home):
    write_config(home)
    manifest = sc.write_workspace(tmp_path / "ws")
    run_id = extracted(home, manifest)
    return home, manifest, run_id


def search(env, *extra, principal="workspace-a", project=PROJECT):
    argv = ["knowledge", "search", "--principal", principal, "--project", project]
    return run_cli([*argv, "--max-tokens", "20000", *extra], env)


def decision_ids(found):
    return {row["id"] for row in found["records"]}


def test_the_registry_is_indexed_by_document_id_and_never_mandatory(workspace):
    env, _, run_id = workspace
    code, result = update_spec(env, run_id)
    assert code == 0, result
    assert result["source"] == f"spec-run:{PROJECT}" and result["complete"] is True
    reasons = {row["reason"] for row in result["skipped"]}
    assert "inferred" in reasons  # the decision and the requirement only the stub infers
    registry = read(env, run_id, "decisions.json")
    inferred = [item["id"] for item in registry["decisions"] if item["basis"] == "inferred"]
    assert inferred and {row["entry"] for row in result["skipped"]} >= set(inferred)

    code, found = search(env)
    assert code == 0 and found["status"] == "ok"
    assert found["mandatory"] == {"count": 0, "tokens": 0, "complete": True, "ids": []}
    assert {row["enforcement"] for row in found["records"]} == {"advisory"}
    assert decision_ids(found) == {rid("ADR-0001"), rid("ADR-0005"), rid("ADR-0006")}


def test_a_chain_returns_its_end_and_an_old_id_names_its_successor(workspace):
    env, _, run_id = workspace
    assert update_spec(env, run_id)[0] == 0
    for old, new in (
        ("ADR-0003", "ADR-0006"),
        ("ADR-0004", "ADR-0006"),
        (sc.HTTP_CLIENT_OLD, sc.HTTP_CLIENT_NEW),
    ):
        code, found = search(env, "--decision-id", old)
        assert code == 0 and found["records"] == [], old
        assert found["superseded"] == [{"asked": old, "by": [rid(new)]}]
    code, found = search(env, "--decision-id", "ADR-0006")
    assert code == 0 and decision_ids(found) == {rid("ADR-0006")} and found["superseded"] == []


def test_proposed_decisions_and_requirements_come_only_apart(workspace):
    env, _, run_id = workspace
    assert update_spec(env, run_id)[0] == 0
    code, found = search(env, "--include-proposed")
    assert code == 0
    proposed = {row["id"] for row in found["proposed"]}
    assert rid(sc.INJECTED_ADR) in proposed
    assert rid(sc.RETENTION_REQ, kind="requirement") in proposed
    assert {row["enforcement"] for row in found["proposed"]} == {"advisory"}
    assert rid(sc.REJECTED_ADR) not in json.dumps(found)


def test_fetch_reads_the_cited_section_back_with_a_matching_hash(workspace):
    env, _, run_id = workspace
    assert update_spec(env, run_id)[0] == 0
    code, fetched = fetch(env, "--principal", "workspace-a", "--id", rid("ADR-0006"))
    assert code == 0 and fetched["status"] == "ok", fetched
    assert fetched["fragments"] and all(part["hash_match"] for part in fetched["fragments"])
    assert all(run_id in part["locator"] for part in fetched["fragments"])
    assert "400" in "".join(part["text"] for part in fetched["fragments"])


def test_a_new_run_updates_the_same_records_and_drops_what_it_no_longer_holds(workspace, tmp_path):
    env, _, run_id = workspace
    assert update_spec(env, run_id)[0] == 0
    code, before = search(env)
    manifest = sc.write_workspace(tmp_path / "ws2")
    (manifest.parent / "docs" / "adr" / "0001-log-format.md").unlink()  # gone from the docs
    later = extracted(env, manifest)
    assert later != run_id
    code, result = update_spec(env, later)
    assert code == 0, result
    assert {row["id"]: row["reason"] for row in result["tombstoned"]} == {
        rid("ADR-0001"): "removed_from_source"
    }
    code, after = search(env)
    assert decision_ids(after) == decision_ids(before) - {rid("ADR-0001")}
    code, fetched = fetch(env, "--principal", "workspace-a", "--id", rid("ADR-0006"))
    assert code == 0 and all(later in part["locator"] for part in fetched["fragments"])


def write_trace(env, run_id, links):
    registry = read(env, run_id, "decisions.json")
    ids = {item.get("external_id"): item["id"] for item in registry["decisions"]}
    rows = []
    for number, (source, target, relation, basis) in enumerate(links, start=1):
        rows.append(
            {
                "schema_version": 1,
                "id": f"TL-{number:04d}",
                "run_id": run_id,
                "created_at": SPEC_CREATED_AT,
                "from": {"type": "decision", "id": ids[source]},
                "to": {"type": "decision", "id": ids[target]},
                "relation": relation,
                "basis": basis,
                "evidence": {"ref": f"{'1' * 40}:docs/adr/x.md#L1"},
                "confidence": "high" if basis == "explicit" else "low",
            }
        )
    value = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at": SPEC_CREATED_AT,
        "default_branch": {"name": "main", "sha": "1" * 40},
        "links": rows,
    }
    records.write_json(paths.child(state_root(env), "runs", run_id, "trace.json"), value)


def test_an_explicit_conflict_is_reported_and_an_inferred_one_is_not(workspace):
    env, _, run_id = workspace
    write_trace(
        env,
        run_id,
        [
            ("ADR-0001", "ADR-0005", "conflicts_with", "explicit"),
            ("ADR-0001", "ADR-0006", "conflicts_with", "inferred"),
        ],
    )
    code, result = update_spec(env, run_id)
    assert code == 0, result
    assert {"entry": "TL-0002", "reason": "inferred"}.items() <= next(
        row for row in result["skipped"] if row["kind"] == "relation"
    ).items()
    code, found = search(env)
    assert code == 0
    assert found["conflicts"] == [
        {"ids": sorted([rid("ADR-0001"), rid("ADR-0005")]), "reason": "conflicts_with"}
    ]


def test_a_project_a_principal_cannot_read_never_reaches_it(workspace):
    env, _, run_id = workspace
    assert update_spec(env, run_id, "project-gamma")[0] == 0
    hidden = rid("ADR-0006", project="project-gamma")
    code, found = search(env, principal="workspace-a", project="project-gamma")
    assert code == 0 and hidden in decision_ids(found)
    for project in ("project-gamma", PROJECT):
        code, found = search(env, "--include-proposed", principal="workspace-b", project=project)
        assert code == 0 and found["records"] == [] and found["proposed"] == []
        assert found["truncated"]["count"] == 0 and "project-gamma" not in json.dumps(found)
    code, fetched = fetch(env, "--principal", "workspace-b", "--id", hidden)
    assert code == 1 and fetched["unfetched"] == [{"reason": "access_denied"}]
    assert fetched["record"] is None and fetched["fragments"] == []
    code, readable = fetch(env, "--principal", "workspace-a", "--id", hidden)
    assert code == 0 and readable["status"] == "ok"
