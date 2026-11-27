import hashlib
import json

import pytest

from sre_harness import records
from sre_harness.knowledge import model
from tests.knowledge_helpers import item

NOW = "2026-11-27T00:00:00Z"


def test_the_id_is_kn_and_twelve_hex_of_the_origin():
    expected = hashlib.sha256(
        json.dumps(["review-registry", "RG-0001"], separators=(",", ":")).encode()
    ).hexdigest()[:12]
    assert model.record_id("review-registry", "RG-0001") == f"KN-{expected}"
    assert model.ID.fullmatch(model.record_id("decision", "project-alpha:ADR-0002"))


def test_the_id_depends_on_the_origin_only():
    first = model.normalize(item("R1"), NOW)
    second = model.normalize(item("R1", summary="別の要約"), "2026-11-28T00:00:00Z")
    assert first["id"] == second["id"]
    assert first["id"] != model.normalize(item("R2"), NOW)["id"]
    assert model.record_id("a", "b-c") != model.record_id("a-b", "c")


def test_a_normalized_item_is_a_valid_record():
    record = model.normalize(item("R1"), NOW)
    assert records.violations("knowledge-record", record) == []
    assert record["indexed_at"] == NOW
    assert record["facets"] == {
        "topic": ["retention"],
        "resource_type": ["queue"],
        "repo": None,
        "path_globs": [],
        "decision_id": None,
    }
    assert record["confidence"] is None


def test_normalize_sorts_readers_and_labels_and_turns_supersedes_into_ids():
    raw = item(
        "R3",
        access={"classification": "internal", "readers": ["team-b", "team-a", "team-b"]},
        facets={"topic": ["timeout", "retention"]},
        supersedes=[{"kind": "synthetic", "key": "R2"}, {"kind": "synthetic", "key": "R1"}],
    )
    record = model.normalize(raw, NOW)
    assert record["access"]["readers"] == ["team-a", "team-b"]
    assert record["facets"]["topic"] == ["retention", "timeout"]
    assert record["supersedes"] == sorted(
        [model.record_id("synthetic", "R1"), model.record_id("synthetic", "R2")]
    )


def test_the_content_hash_covers_the_normative_fields_only():
    base = model.normalize(item("R1"), NOW)
    assert base["content_hash"] == records.fingerprint(
        {name: base[name] for name in model.NORMATIVE}
    )
    moved = model.normalize(
        item("R1", evidence=[], facets={"topic": ["other"]}, updated_at="2026-11-02T00:00:00Z"),
        NOW,
    )
    assert moved["content_hash"] == base["content_hash"]
    for change in (
        {"summary": "変えた"},
        {"applies_when": ["別の条件"]},
        {"exceptions": ["例外"]},
        {"status": "retired"},
        {"enforcement": "mandatory"},
        {"access": {"classification": "public", "readers": ["team-alpha"]}},
    ):
        assert model.normalize(item("R1", **change), NOW)["content_hash"] != base["content_hash"]


def test_the_record_hash_sees_every_field_but_the_index_time():
    base = model.normalize(item("R1"), NOW)
    later = model.normalize(item("R1"), "2026-12-01T00:00:00Z")
    assert model.record_hash(base) == model.record_hash(later)
    moved = model.normalize(item("R1", facets={"topic": ["other"]}), NOW)
    assert model.record_hash(moved) != model.record_hash(base)


SCOPES = [
    ({"level": "organization", "project_id": None, "component_id": None, "task_id": None}, True),
    ({"level": "organization", "project_id": "p", "component_id": None, "task_id": None}, False),
    ({"level": "project", "project_id": "p", "component_id": None, "task_id": None}, True),
    ({"level": "project", "project_id": None, "component_id": None, "task_id": None}, False),
    ({"level": "project", "project_id": "p", "component_id": "c", "task_id": None}, False),
    ({"level": "component", "project_id": "p", "component_id": "c", "task_id": None}, True),
    ({"level": "component", "project_id": "p", "component_id": "c", "task_id": "t"}, False),
    ({"level": "component", "project_id": None, "component_id": "c", "task_id": None}, False),
    ({"level": "task", "project_id": "p", "component_id": "c", "task_id": "t"}, True),
    ({"level": "task", "project_id": "p", "component_id": None, "task_id": "t"}, False),
]


@pytest.mark.parametrize(("scope", "fine"), SCOPES)
def test_ids_below_the_level_are_null_and_ids_at_or_above_are_set(scope, fine):
    problems = model.problems(model.normalize(item("R1", scope=scope), NOW))
    assert (problems == []) is fine
    if not fine:
        assert any("scope" in problem for problem in problems)


@pytest.mark.parametrize("status", ["approved", "superseded"])
def test_an_approved_or_superseded_record_needs_an_approval(status):
    problems = model.problems(model.normalize(item("R1", status=status, approval=None), NOW))
    assert problems and "approval" in problems[0]


@pytest.mark.parametrize("status", ["proposed", "rejected", "retired", "unresolved"])
def test_other_statuses_need_no_approval(status):
    assert model.problems(model.normalize(item("R1", status=status, approval=None), NOW)) == []


@pytest.mark.parametrize("status", ["proposed", "superseded", "retired", "rejected", "unresolved"])
def test_only_an_approved_record_can_be_mandatory(status):
    record = model.normalize(item("R1", status=status, enforcement="mandatory"), NOW)
    assert any("mandatory" in problem for problem in model.problems(record))
    approved = model.normalize(item("R1", enforcement="mandatory"), NOW)
    assert model.problems(approved) == []


def test_a_cycle_in_supersedes_is_found_and_named():
    assert model.supersession_cycle({"a": ["b"], "b": ["c"], "c": []}) is None
    assert model.supersession_cycle({"a": ["b"], "b": ["missing"]}) is None
    cycle = model.supersession_cycle({"a": ["b"], "b": ["c"], "c": ["a"], "d": ["a"]})
    assert cycle is not None and sorted(cycle[:-1]) == ["a", "b", "c"] and cycle[0] == cycle[-1]
    assert model.supersession_cycle({"a": ["a"]}) == ["a", "a"]


# Schemas ------------------------------------------------------------------------------------

SEARCH_RESULT = {
    "schema_version": 1,
    "status": "ok",
    "snapshot": {"generation": 42, "updated_at": "2026-11-27T00:00:00Z"},
    "principal": "workspace-a",
    "retrieval": {
        "method": "fts5",
        "query_terms": 3,
        "expansion": {"source": "caller", "terms": 4},
    },
    "budget": {
        "estimator": "utf8_bytes_div_3",
        "max_tokens": 1500,
        "used_tokens": 1180,
        "task": {"id": "TASK-0001", "limit": 6000, "used_before": 0, "used_after": 1180},
    },
    "mandatory": {
        "count": 2,
        "tokens": 410,
        "complete": True,
        "ids": ["KN-3f2a9c01b7de", "KN-000000000005"],
    },
    "records": [
        {
            "id": "KN-3f2a9c01b7de",
            "kind": "rule",
            "enforcement": "mandatory",
            "status": "approved",
            "scope": {"level": "organization"},
            "summary": "外向きの HTTP 呼び出しには timeout を設定する",
            "applies_when": ["HTTP クライアントを新しく作る"],
            "exceptions": ["ストリーム受信"],
            "confidence": {"value": "strong", "vocabulary": "review"},
            "evidence": [
                {
                    "locator": "registry:RG-0001/v0002.md",
                    "revision": "v0002",
                    "content_sha256": "a" * 64,
                    "size": 812,
                }
            ],
            "freshness": "fresh",
            "why_selected": ["mandatory", "path_match"],
        }
    ],
    "proposed": [],
    "truncated": {"count": 1, "ids": ["KN-000000000001"], "tokens_needed": 2300},
    "unfetched": [],
    "conflicts": [
        {"ids": ["KN-000000000002", "KN-000000000003"], "reason": "both_approved_same_subject"}
    ],
    "freshness": {"stale": 1, "unknown": 0, "sources_failed": []},
    "superseded": [{"asked": "ADR-0002", "by": ["KN-000000000004"]}],
}


def test_the_design_example_of_a_search_result_is_valid():
    assert records.violations("knowledge-search-result", SEARCH_RESULT) == []


def test_a_search_result_without_an_expansion_is_valid():
    value = json.loads(json.dumps(SEARCH_RESULT))
    value["retrieval"] = {"method": "fts5", "query_terms": 0, "expansion": None}
    assert records.violations("knowledge-search-result", value) == []


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(status="partial"),
        lambda v: v["budget"].update(estimator="tiktoken"),
        lambda v: v["records"][0].update(body="the source text"),
        lambda v: v["records"][0]["scope"].update(level="galaxy"),
        lambda v: v["conflicts"][0].update(ids=["KN-000000000002"]),
        lambda v: v["unfetched"].append({"id": "KN-000000000005", "reason": "made_up"}),
        lambda v: v.pop("proposed"),
        lambda v: v["mandatory"].pop("ids"),
        lambda v: v.pop("retrieval"),
        lambda v: v["retrieval"].update(method="vector"),
        lambda v: v["retrieval"].update(expansion={"source": "model", "terms": 1}),
        lambda v: v["retrieval"].update(query_terms=-1),
    ],
)
def test_a_search_result_outside_the_contract_is_invalid(change):
    value = json.loads(json.dumps(SEARCH_RESULT))
    change(value)
    assert records.violations("knowledge-search-result", value) != []


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(id="KN-XYZ"),
        lambda v: v.update(summary="二行\nの要約"),
        lambda v: v.update(summary="x" * 241),
        lambda v: v["access"].update(readers=[]),
        lambda v: v.pop("access"),
        lambda v: v.update(confidence={"value": "strong", "vocabulary": "spec"}),
    ],
)
def test_a_record_outside_the_contract_is_invalid(change):
    value = model.normalize(item("R1"), NOW)
    change(value)
    assert records.violations("knowledge-record", value) != []


def test_an_item_without_access_is_still_a_well_formed_item():
    value = item("R1")
    del value["access"]
    assert records.violations("knowledge-record", value, "item") == []
