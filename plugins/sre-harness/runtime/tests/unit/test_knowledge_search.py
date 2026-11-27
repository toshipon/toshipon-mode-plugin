"""The pure parts of `knowledge search`: scope, facets, the mandatory set, ranking, the budget,
supersession, conflicts and freshness. Records are synthetic and built in memory."""

import pytest

from sre_harness import records
from sre_harness.knowledge import model, search, terms
from sre_harness.review import markdown
from tests.knowledge_helpers import item

NOW = "2026-11-27T00:00:00Z"
SOURCE = "file:synthetic"
SOURCES = {SOURCE: {"state": "complete", "last_success_at": NOW}}


def rec(key, **changes):
    return model.normalize(item(key, **changes), NOW)


def rid(key):
    return model.record_id("synthetic", key)


def scope(level, project=None, component=None, task=None):
    return {"level": level, "project_id": project, "component_id": component, "task_id": task}


ORG = scope("organization")
PROJECT = scope("project", "project-alpha")
COMPONENT = scope("component", "project-alpha", "queue")
TASK = scope("task", "project-alpha", "queue", "TASK-0001")
OTHER_COMPONENT = scope("component", "project-alpha", "api")


def select(rows, request=None, *, matches=None, sources=SOURCES, edges=(), now=NOW, origin=None):
    request = request or search.Request(project="project-alpha", component="queue")
    provenance = {row["id"]: (origin or {}).get(row["id"], SOURCE) for row in rows}
    return search.select(
        rows,
        request=request,
        matches=matches or {},
        provenance=provenance,
        sources=sources,
        conflict_edges=list(edges),
        now=now,
    )


def ids(views):
    return [view["id"] for view in views]


# Scope and facets ------------------------------------------------------------------------


def test_scope_is_the_organization_and_the_given_chain_only():
    rows = {
        "org": rec("org", scope=ORG),
        "project": rec("project", scope=PROJECT),
        "component": rec("component", scope=COMPONENT),
        "task": rec("task", scope=TASK),
        "other": rec("other", scope=OTHER_COMPONENT),
        "beta": rec("beta", scope=scope("project", "project-beta")),
    }

    def kept(**given):
        request = search.Request(**given)
        return {key for key, row in rows.items() if search.in_scope(row, request)}

    assert kept() == {"org"}
    assert kept(project="project-alpha") == {"org", "project"}
    assert kept(project="project-alpha", component="queue") == {"org", "project", "component"}
    assert kept(project="project-alpha", component="queue", task="TASK-0001") == {
        "org",
        "project",
        "component",
        "task",
    }
    assert kept(project="project-alpha", component="queue", task="TASK-0002") == {
        "org",
        "project",
        "component",
    }
    # A task id alone matches no task record: a task record always names its component.
    assert kept(project="project-alpha", task="TASK-0001") == {"org", "project"}


def test_a_record_without_a_facet_matches_it_and_a_request_without_it_excludes_nothing():
    bare = rec("bare", facets={})
    shaped = rec(
        "shaped",
        facets={
            "repo": "example-org/alpha",
            "path_globs": ["infra/queue/**"],
            "resource_type": ["queue"],
        },
    )
    given = search.Request(
        repo="example-org/alpha", paths=("infra/queue/main.tf",), resource_types=("queue",)
    )
    assert search.facet_match(bare, given) == (True, False, False)
    assert search.facet_match(shaped, given) == (True, True, True)
    assert search.facet_match(shaped, search.Request()) == (True, False, False)
    assert search.facet_match(shaped, search.Request(repo="example-org/beta"))[0] is False
    assert search.facet_match(shaped, search.Request(paths=("infra/api/main.tf",)))[0] is False
    assert search.facet_match(shaped, search.Request(resource_types=("bucket",)))[0] is False
    # `*` stays within one directory, `**` spans them, as in the declarative checks.
    star = rec("star", facets={"path_globs": ["infra/*.tf"]})
    assert search.facet_match(star, search.Request(paths=("infra/main.tf",)))[1] is True
    assert search.facet_match(star, search.Request(paths=("infra/queue/main.tf",)))[0] is False


# The mandatory set and the budget ----------------------------------------------------------


def mandatory(key, **changes):
    return rec(key, enforcement="mandatory", **changes)


def test_the_mandatory_set_does_not_depend_on_the_query():
    rows = [
        mandatory("M1", summary="外向きの HTTP 呼び出しには timeout を設定する"),
        mandatory("M2", scope=ORG, summary="保存時に暗号化する"),
        rec("A1", summary="キューの保持期間は 14 日にする"),
    ]
    results = []
    for query, matches in [
        ("", {}),
        ("保持期間", {rid("A1"): -1.0}),
        ("全く関係のない語", {}),
        ("timeout", {rid("M1"): -2.0}),
    ]:
        request = search.Request(project="project-alpha", component="queue", query=query)
        result = select(rows, request, matches=matches)
        mandatory_views = [v for v in result["records"] if v["enforcement"] == "mandatory"]
        results.append((result["mandatory"], mandatory_views))
    assert all(found == results[0] for found in results)
    assert results[0][0]["ids"] == sorted([rid("M1"), rid("M2")])


def test_mandatory_records_that_do_not_fit_stop_the_search():
    rows = [mandatory(f"M{n}", applies_when=[f"条件 {n} " * 20]) for n in range(1, 6)]
    rows.append(rec("A1"))
    request = search.Request(project="project-alpha", max_tokens=100)
    result = select(rows, request)
    assert result["status"] == "mandatory_overflow"
    assert result["records"] == [] and result["proposed"] == []
    assert result["mandatory"]["complete"] is False
    assert result["mandatory"]["ids"] == sorted(rid(f"M{n}") for n in range(1, 6))
    assert result["mandatory"]["tokens"] > 100
    assert result["budget"]["used_tokens"] == 0
    assert result["truncated"] == {"count": 0, "ids": [], "tokens_needed": 0}

    enough = search.Request(project="project-alpha", max_tokens=result["mandatory"]["tokens"])
    again = select(rows, enough)
    assert again["status"] == "ok" and again["mandatory"]["complete"] is True
    assert again["mandatory"]["tokens"] == result["mandatory"]["tokens"]
    assert set(again["mandatory"]["ids"]) <= set(ids(again["records"]))
    # The advisory record did not fit in what was left: it is counted, not dropped silently.
    assert again["truncated"]["ids"] == [rid("A1")]


def test_the_budget_is_the_canonical_json_of_what_is_returned():
    rows = [rec(f"A{n}", summary=f"規則 {n} " + "保持期間 " * n) for n in range(1, 20)]
    rows.append(mandatory("M1"))
    request = search.Request(project="project-alpha", max_tokens=600, include_proposed=True)
    rows.append(rec("P1", status="proposed", approval=None))
    result = select(rows, request)
    returned = result["records"] + result["proposed"]
    counted = sum(terms.estimate_tokens(records.canonical(view)) for view in returned)
    assert result["budget"]["used_tokens"] == counted <= 600
    assert all(
        search.tokens(view) == terms.estimate_tokens(records.canonical(view)) for view in returned
    )
    assert result["truncated"]["count"] == len(result["truncated"]["ids"]) > 0
    assert result["truncated"]["count"] + len(returned) == len(rows)
    assert result["budget"]["task"] is None
    assert result["budget"]["estimator"] == terms.ESTIMATOR


def test_filling_stops_at_the_first_record_that_does_not_fit():
    big = rec(
        "A1", summary="保持期間 " * 40, confidence={"value": "strong", "vocabulary": "review"}
    )
    small = rec("A2", summary="小さい", confidence={"value": "candidate", "vocabulary": "review"})
    budget = search.tokens(search.view(small, ["scope_match"], "fresh")) + 1
    result = select([big, small], search.Request(project="project-alpha", max_tokens=budget))
    assert result["records"] == []
    assert result["truncated"]["ids"] == [rid("A1"), rid("A2")]


# Ranking ---------------------------------------------------------------------------------


def test_a_more_specific_scope_ranks_first():
    rows = [rec("org", scope=ORG), rec("project", scope=PROJECT), rec("component", scope=COMPONENT)]
    result = select(rows)
    assert ids(result["records"]) == [rid("component"), rid("project"), rid("org")]


def test_path_and_resource_type_matches_rank_before_terms():
    matched = rec("matched", facets={"path_globs": ["infra/queue/**"], "resource_type": ["queue"]})
    worded = rec("worded", facets={})
    request = search.Request(
        project="project-alpha",
        query="保持",
        paths=("infra/queue/a.tf",),
        resource_types=("queue",),
    )
    result = select([matched, worded], request, matches={rid("matched"): -0.1, rid("worded"): -9.0})
    assert ids(result["records"]) == [rid("matched"), rid("worded")]
    assert result["records"][0]["why_selected"] == [
        "path_match",
        "resource_type_match",
        "term_match",
    ]


def test_bm25_then_confidence_then_recency_then_the_id():
    def row(key, confidence, updated_at):
        return rec(
            key,
            facets={},
            confidence={"value": confidence, "vocabulary": "review"},
            updated_at=updated_at,
        )

    rows = [
        row("e", "candidate", "2026-01-01T00:00:00Z"),
        row("d", "candidate", "2026-01-01T00:00:00Z"),
        row("c", "candidate", "2026-06-01T00:00:00Z"),
        row("b", "strong", "2026-01-01T00:00:00Z"),
        row("a", "candidate", "2026-01-01T00:00:00Z"),
    ]
    matches = {rid(key): -1.0 for key in "bcde"}
    matches[rid("a")] = -5.0
    result = select(rows, search.Request(project="project-alpha", query="保持"), matches=matches)
    tied = sorted([rid("d"), rid("e")])
    assert ids(result["records"]) == [rid("a"), rid("b"), rid("c"), *tied]


def test_with_a_query_only_records_sharing_a_term_are_ranked():
    rows = [rec("hit"), rec("miss")]
    request = search.Request(project="project-alpha", query="保持")
    result = select(rows, request, matches={rid("hit"): -1.0})
    assert ids(result["records"]) == [rid("hit")]
    # A query with no searchable term (one character, punctuation) is no query.
    assert len(select(rows, search.Request(project="project-alpha", query='" ( *'))["records"]) == 2


@pytest.mark.parametrize(
    ("confidence", "rank"),
    [
        ({"value": "strong", "vocabulary": "review"}, 3),
        ({"value": "recurring", "vocabulary": "review"}, 2),
        ({"value": "candidate", "vocabulary": "review"}, 1),
        ({"value": "high", "vocabulary": "spec"}, 3),
        ({"value": "medium", "vocabulary": "spec"}, 2),
        ({"value": "low", "vocabulary": "spec"}, 1),
        (None, 0),
    ],
)
def test_the_two_confidence_vocabularies_share_one_scale(confidence, rank):
    assert search.confidence_rank(confidence) == rank


def test_topic_and_decision_id_narrow_the_advisory_records_only():
    rows = [
        mandatory("M1", facets={"topic": ["encryption"]}),
        rec("retention", facets={"topic": ["retention"]}),
        rec("timeout", facets={"topic": ["timeout"]}),
        rec("adr", facets={"topic": ["timeout"], "decision_id": "ADR-0002"}),
    ]
    by_topic = select(rows, search.Request(project="project-alpha", topics=("retention",)))
    assert set(ids(by_topic["records"])) == {rid("M1"), rid("retention")}
    by_decision = select(rows, search.Request(project="project-alpha", decision_id="ADR-0002"))
    assert set(ids(by_decision["records"])) == {rid("M1"), rid("adr")}


# Supersession ----------------------------------------------------------------------------


def chain():
    first = rec("v1", status="superseded", facets={"decision_id": "ADR-0001"})
    second = rec(
        "v2",
        status="superseded",
        facets={"decision_id": "ADR-0002"},
        supersedes=[{"kind": "synthetic", "key": "v1"}],
    )
    third = rec(
        "v3", facets={"decision_id": "ADR-0003"}, supersedes=[{"kind": "synthetic", "key": "v2"}]
    )
    return first, second, third


def test_only_the_end_of_a_chain_is_returned():
    result = select(list(chain()))
    assert ids(result["records"]) == [rid("v3")]
    assert result["superseded"] == []


def test_an_approved_record_with_an_approved_successor_is_replaced():
    # The source left the old one approved; the edge and the successor's status decide.
    old = rec("old", facets={"decision_id": "ADR-0001"})
    new = rec("new", supersedes=[{"kind": "synthetic", "key": "old"}])
    assert search.replacements([old, new]) == {rid("old"): (rid("new"),)}
    assert ids(select([old, new])["records"]) == [rid("new")]
    # A proposed or rejected successor replaces nothing.
    for status, approval in (("proposed", None), ("rejected", None), ("retired", None)):
        pending = rec(
            "new",
            status=status,
            approval=approval,
            supersedes=[{"kind": "synthetic", "key": "old"}],
        )
        assert search.replacements([old, pending]) == {}


def test_asking_for_a_superseded_decision_returns_its_successor():
    request = search.Request(project="project-alpha", decision_id="ADR-0001")
    result = select(list(chain()), request)
    assert result["records"] == []
    assert result["superseded"] == [{"asked": "ADR-0001", "by": [rid("v3")]}]
    middle = select(list(chain()), search.Request(project="project-alpha", decision_id="ADR-0002"))
    assert middle["superseded"] == [{"asked": "ADR-0002", "by": [rid("v3")]}]
    live = select(list(chain()), search.Request(project="project-alpha", decision_id="ADR-0003"))
    assert ids(live["records"]) == [rid("v3")] and live["superseded"] == []


def test_a_successor_the_principal_cannot_read_is_not_revealed():
    # The reader only hands over readable records, so the successor is simply absent.
    first, second, _ = chain()
    request = search.Request(project="project-alpha", decision_id="ADR-0001")
    result = select([first, second], request)
    assert result["superseded"] == [] and result["records"] == []
    old = rec("old", facets={"decision_id": "ADR-0001"})
    seen = select([old], request)
    assert ids(seen["records"]) == [rid("old")] and seen["superseded"] == []


# Conflicts -------------------------------------------------------------------------------


def test_conflicts_are_reported_without_a_verdict():
    rows = [
        rec("a", facets={"topic": ["timeout"]}, summary="タイムアウトは 30 分"),
        rec("b", facets={"topic": ["timeout"]}, summary="タイムアウトは 2 時間"),
        rec("c", facets={"topic": ["retention"], "decision_id": "ADR-0009"}),
        rec("d", scope=COMPONENT, facets={"topic": ["retention"], "decision_id": "ADR-0009"}),
        rec("e", scope=COMPONENT, facets={"topic": ["backup"]}),
        rec("f", scope=ORG, facets={"topic": ["timeout"]}),
    ]
    result = select(rows, edges=[(rid("e"), rid("f"))])
    expected = [
        {"ids": sorted([rid("a"), rid("b")]), "reason": "both_approved_same_subject"},
        {"ids": sorted([rid("c"), rid("d")]), "reason": "both_approved_same_subject"},
        {"ids": sorted([rid("e"), rid("f")]), "reason": "conflicts_with"},
    ]
    assert result["conflicts"] == sorted(expected, key=lambda c: (c["reason"], c["ids"]))
    assert all(view["status"] == "approved" for view in result["records"])


def test_conflicts_only_count_what_was_returned():
    rows = [
        rec("a", facets={"topic": ["timeout"]}),
        rec("b", facets={"topic": ["timeout"]}, summary="保持期間 " * 40),
    ]
    budget = search.tokens(search.view(rows[0], ["scope_match"], "fresh"))
    result = select(rows, search.Request(project="project-alpha", max_tokens=budget + 5))
    assert len(result["records"]) == 1 and result["conflicts"] == []
    dangling = select(rows, edges=[(rid("a"), "KN-000000000000")])["conflicts"]
    assert [found["reason"] for found in dangling] == ["both_approved_same_subject"]


# Proposed --------------------------------------------------------------------------------


def test_proposed_records_come_apart_and_only_when_asked():
    rows = [rec("A1"), rec("P1", status="proposed", approval=None)]
    assert ids(select(rows)["records"]) == [rid("A1")] and select(rows)["proposed"] == []
    asked = select(rows, search.Request(project="project-alpha", include_proposed=True))
    assert ids(asked["records"]) == [rid("A1")]
    assert ids(asked["proposed"]) == [rid("P1")]
    assert asked["proposed"][0]["status"] == "proposed"
    assert asked["mandatory"]["ids"] == []


# Freshness -------------------------------------------------------------------------------


HOUR = 3600


def later(hours):
    from datetime import datetime, timedelta, timezone

    start = datetime(2026, 11, 27, tzinfo=timezone.utc)
    return records.iso(start + timedelta(hours=hours))


@pytest.mark.parametrize(
    ("hours", "expected"),
    [(0, "fresh"), (search.STALE_AFTER_HOURS, "fresh"), (search.STALE_AFTER_HOURS + 1, "stale")],
)
def test_freshness_is_the_age_of_the_last_successful_update(hours, expected):
    row = rec("A1")
    assert search.freshness(row, SOURCE, SOURCES, later(hours), search.STALE_AFTER_HOURS) == (
        expected
    )


def test_freshness_is_unknown_when_nothing_confirms_it():
    row = rec("A1")
    assert search.freshness(row, "file:gone", SOURCES, NOW, 24) == "unknown"
    failed = {SOURCE: {"state": "failed", "last_success_at": NOW}}
    assert search.freshness(row, SOURCE, failed, NOW, 24) == "unknown"
    # A clock behind the last update cannot tell the age.
    assert search.freshness(row, SOURCE, SOURCES, "2026-11-26T00:00:00Z", 24) == "unknown"


def test_a_partial_source_only_vouches_for_what_it_wrote():
    row = model.normalize(item("A1"), "2026-11-01T00:00:00Z")
    partial = {SOURCE: {"state": "partial", "last_success_at": NOW}}
    assert search.freshness(row, SOURCE, partial, NOW, 24) == "stale"
    assert search.freshness(row, SOURCE, SOURCES, NOW, 24) == "fresh"


def test_the_freshness_block_counts_what_was_returned_and_names_failed_sources():
    rows = [rec("A1"), rec("A2")]
    sources = {SOURCE: {"state": "complete", "last_success_at": "2026-10-01T00:00:00Z"}}
    stale = select(rows, sources=sources)
    assert stale["freshness"] == {"stale": 2, "unknown": 0, "sources_failed": []}
    assert {view["freshness"] for view in stale["records"]} == {"stale"}
    failed = {SOURCE: {"state": "failed", "last_success_at": NOW}}
    result = select(rows, sources=failed)
    assert result["freshness"] == {"stale": 0, "unknown": 2, "sources_failed": [SOURCE]}


# The returned view -----------------------------------------------------------------------


def test_the_view_drops_ids_below_the_level_and_cleans_control_characters():
    row = rec(
        "A1",
        scope=COMPONENT,
        summary="保持期間\x1b[31m は\u202e 14 日",
        applies_when=["キュー\u2066を作る"],
        exceptions=["\x07例外"],
    )
    view = search.view(row, ["scope_match"], "fresh")
    assert view["scope"] == {
        "level": "component",
        "project_id": "project-alpha",
        "component_id": "queue",
    }
    assert search.view(rec("O", scope=ORG), ["scope_match"], "fresh")["scope"] == {
        "level": "organization"
    }
    text = records.canonical(view)
    assert not markdown._CONTROL.search(text)
    assert view["summary"] == markdown.clean(row["summary"])
    assert set(view) == set(
        records.schema("knowledge-search-result")["$defs"]["record"]["required"]
    )


def test_the_same_input_gives_the_same_result():
    rows = [rec(f"A{n}") for n in range(10)] + [mandatory("M1")]
    first = select(rows, search.Request(project="project-alpha", max_tokens=700))
    second = select(list(reversed(rows)), search.Request(project="project-alpha", max_tokens=700))
    assert records.canonical(first) == records.canonical(second)
