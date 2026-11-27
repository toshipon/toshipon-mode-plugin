"""`sre-harness knowledge search` end to end, through `cli.main`, on the synthetic corpus."""

import io
import json
import shutil

import pytest

from examples.synthetic import knowledge_corpus as kc
from sre_harness import cli, paths, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import ingest, model, search, terms
from sre_harness.knowledge.adapters import build
from sre_harness.review import markdown
from tests.knowledge_helpers import CONFIG, item, update, write_config, write_input

INDEXED = "2026-11-27T00:00:00Z"
SEARCHED = "2026-11-27T06:00:00Z"
SCHEMA = "knowledge-search-result"


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    home = tmp_path_factory.mktemp("search")
    env = {"AI_HARNESS_HOME": str(home / "state")}
    root = paths.state_root(env)
    scenario = kc.scenario(300)
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    path = home / "corpus.json"
    path.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    ingest.update(root, build("file", {"input": path}), now=INDEXED)
    return env, scenario


@pytest.fixture(autouse=True)
def no_task_total(request):
    """`--task` also adds to the task total (knowledge/tasks/). Each test starts without one,
    so the module's shared corpus does not run out of budget across tests."""
    if "corpus" in request.fixturenames:
        env, _ = request.getfixturevalue("corpus")
        shutil.rmtree(paths.state_root(env) / "knowledge" / "tasks", ignore_errors=True)


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    moment = {"now": SEARCHED}
    monkeypatch.setattr(knowledge_cli, "clock", lambda: moment["now"])
    return moment


def raw(env, *argv):
    """(exit code, stdout text)."""
    out = io.StringIO()
    code = cli.main(["knowledge", "search", *argv], env=env, out=out)
    return code, out.getvalue()


def run(env, *argv):
    code, text = raw(env, *argv)
    value = json.loads(text) if text.strip() else None
    if value is not None:
        assert records.violations(SCHEMA, value) == [], value
    return code, value


def task_args(task, *extra):
    return [
        "--principal",
        task["principal"],
        "--project",
        task["project_id"],
        "--component",
        task["component_id"],
        "--task",
        task["task_id"],
        "--query",
        task["query"],
        *[part for path in task["paths"] for part in ("--path", path)],
        *[part for kind in task["resource_types"] for part in ("--resource-type", kind)],
        *extra,
    ]


def by_id(scenario):
    return {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]): i
        for i in scenario.corpus["items"]
    }


# Budget ----------------------------------------------------------------------------------


def test_every_task_gets_exactly_its_mandatory_records(corpus):
    env, scenario = corpus
    for task in scenario.tasks:
        code, result = run(env, *task_args(task))
        assert result["mandatory"]["ids"] == task["mandatory"], task["task_id"]
        assert result["mandatory"]["tokens"] == task["mandatory_tokens"], task["task_id"]
        if task["task_id"] == scenario.traps["overflow_task"]:
            assert code == 1 and result["status"] == "mandatory_overflow"
        else:
            assert code == 0 and result["status"] == "ok", task["task_id"]
            returned = {view["id"] for view in result["records"]}
            assert set(task["mandatory"]) <= returned
            assert returned <= set(task["applicable"]), task["task_id"]


def test_the_overflowing_task_stops_and_goes_on_with_a_larger_budget(corpus, capsys):
    env, scenario = corpus
    (task,) = [t for t in scenario.tasks if t["task_id"] == scenario.traps["overflow_task"]]
    code, result = run(env, *task_args(task))
    assert code == 1 and result["status"] == "mandatory_overflow"
    assert result["records"] == [] and result["proposed"] == []
    assert result["mandatory"]["complete"] is False
    assert result["mandatory"]["tokens"] > kc.BUDGET == result["budget"]["max_tokens"]
    assert len(result["mandatory"]["ids"]) == kc.OVERFLOW_MANDATORY + 2
    assert result["budget"]["used_tokens"] == 0
    bigger = str(result["mandatory"]["tokens"] + 400)
    code, again = run(env, *task_args(task, "--max-tokens", bigger))
    assert code == 0 and again["status"] == "ok" and again["mandatory"]["complete"] is True
    assert set(again["mandatory"]["ids"]) <= {view["id"] for view in again["records"]}
    assert again["budget"]["used_tokens"] <= int(bigger)


def test_what_does_not_fit_is_counted(corpus):
    env, scenario = corpus
    task = scenario.tasks[1]
    code, result = run(env, *task_args(task, "--query", "", "--max-tokens", "900"))
    assert code == 0
    returned = result["records"] + result["proposed"]
    assert result["truncated"]["count"] == len(result["truncated"]["ids"]) > 0
    assert result["budget"]["used_tokens"] == sum(
        terms.estimate_tokens(records.canonical(view)) for view in returned
    )
    assert result["budget"]["used_tokens"] <= 900
    assert not {view["id"] for view in returned} & set(result["truncated"]["ids"])
    assert result["budget"]["task"] is None


# Leaks -----------------------------------------------------------------------------------


def gamma(scenario):
    found = [i for i in scenario.corpus["items"] if i["scope"]["project_id"] == kc.THIRD_PROJECT]
    return [model.record_id(i["origin"]["kind"], i["origin"]["key"]) for i in found], [
        i["summary"] for i in found
    ]


@pytest.mark.parametrize("extra", [[], ["--include-proposed"], ["--max-tokens", "300"]])
def test_the_limited_principal_sees_nothing_of_the_third_project(corpus, capsys, extra):
    env, scenario = corpus
    hidden_ids, hidden_summaries = gamma(scenario)
    assert hidden_ids
    for component in kc.PROJECTS[kc.THIRD_PROJECT]["components"]:
        argv = [
            "--principal",
            kc.PRINCIPAL_LIMITED,
            "--project",
            kc.THIRD_PROJECT,
            "--component",
            component,
            "--query",
            "保持期間 バックアップ タイムアウト",
            "--path",
            f"services/{component}/main.tf",
            *extra,
        ]
        code, text = raw(env, *argv)
        stderr = capsys.readouterr().err
        assert code in (0, 1)
        everything = text + stderr
        for record_id in hidden_ids:
            assert record_id not in everything
        for summary in hidden_summaries:
            assert summary not in everything
        assert kc.THIRD_PROJECT not in everything and "team-gamma" not in everything


def test_the_two_principals_differ_only_by_what_one_cannot_read(corpus):
    env, scenario = corpus
    hidden = set(gamma(scenario)[0])
    for component in ("billing", "auth"):
        common = ["--project", kc.THIRD_PROJECT, "--component", component]
        common += ["--max-tokens", "100000", "--include-proposed"]
        _, wide = run(env, "--principal", kc.PRINCIPAL_ALL, *common)
        _, narrow = run(env, "--principal", kc.PRINCIPAL_LIMITED, *common)
        for part in ("records", "proposed"):
            expected = [v for v in wide[part] if v["id"] not in hidden]
            assert narrow[part] == expected
        assert {v["id"] for v in wide["records"]} & hidden
        assert (
            narrow["truncated"] == wide["truncated"] == {"count": 0, "ids": [], "tokens_needed": 0}
        )
        narrow_conflicts = [c for c in wide["conflicts"] if not set(c["ids"]) & hidden]
        assert narrow["conflicts"] == narrow_conflicts
    # Where nothing is hidden the two answers are the same but for the principal.
    task = next(t for t in scenario.tasks if t["project_id"] == "project-alpha")
    _, first = run(env, *task_args({**task, "principal": kc.PRINCIPAL_ALL}))
    _, second = run(env, *task_args({**task, "principal": kc.PRINCIPAL_LIMITED}))
    assert {**first, "principal": None} == {**second, "principal": None}


def test_an_unknown_principal_exits_3_and_names_no_known_one(corpus, capsys):
    env, _ = corpus
    code, text = raw(env, "--principal", "nobody", "--project", "project-alpha")
    stderr = capsys.readouterr().err
    assert code == 3 and text == ""
    assert "nobody" in stderr
    for name in (kc.PRINCIPAL_ALL, kc.PRINCIPAL_LIMITED, "team-alpha", "project-alpha"):
        assert name not in stderr


def test_the_principal_is_required(corpus, capsys):
    env, _ = corpus
    code, text = raw(env, "--project", "project-alpha")
    assert code == 2 and text == ""
    assert "--principal" in capsys.readouterr().err


def test_no_config_exits_3(home):
    code, text = raw(home, "--principal", "workspace-a")
    assert code == 3 and text == ""


# Old decisions ---------------------------------------------------------------------------


def test_the_supersession_chain_returns_its_end_only(corpus):
    env, scenario = corpus
    first, second, last = scenario.traps["supersession_chain"]
    argv = ["--principal", kc.PRINCIPAL_ALL, "--project", "project-alpha", "--component", "queue"]
    code, result = run(env, *argv, "--max-tokens", "100000", "--include-proposed")
    returned = {view["id"] for view in result["records"] + result["proposed"]}
    assert code == 0 and last in returned
    assert first not in returned and second not in returned
    assert first not in result["truncated"]["ids"] and second not in result["truncated"]["ids"]


DECISIONS = [
    item(
        "D1",
        status="superseded",
        summary="D1: 保持期間は 30 日",
        facets={"topic": ["retention"], "decision_id": "ADR-0001"},
    ),
    item(
        "D2",
        summary="D2: 保持期間は 90 日",
        facets={"topic": ["retention"], "decision_id": "ADR-0002"},
        supersedes=[{"kind": "synthetic", "key": "D1"}],
    ),
    item(
        "H1",
        status="superseded",
        summary="H1: 隠れた旧決定",
        facets={"topic": ["backup"], "decision_id": "ADR-0101"},
    ),
    item(
        "H2",
        summary="H2: 読めない後継",
        facets={"topic": ["backup"], "decision_id": "ADR-0102"},
        access={"classification": "confidential", "readers": ["team-gamma"]},
        supersedes=[{"kind": "synthetic", "key": "H1"}],
    ),
]


@pytest.fixture
def decisions(home, tmp_path):
    write_config(home)
    code, _ = update(home, write_input(tmp_path / "in.json", DECISIONS))
    assert code == 0
    return home


def test_a_superseded_decision_id_answers_with_its_successor(decisions):
    code, result = run(
        decisions,
        "--principal",
        "workspace-b",
        "--project",
        "project-alpha",
        "--decision-id",
        "ADR-0001",
    )
    assert code == 0 and result["records"] == []
    assert result["superseded"] == [
        {"asked": "ADR-0001", "by": [model.record_id("synthetic", "D2")]}
    ]
    code, live = run(
        decisions,
        "--principal",
        "workspace-b",
        "--project",
        "project-alpha",
        "--decision-id",
        "ADR-0002",
    )
    assert [v["id"] for v in live["records"]] == [model.record_id("synthetic", "D2")]
    assert live["superseded"] == []


def test_a_successor_the_principal_cannot_read_does_not_leak(decisions, capsys):
    hidden = model.record_id("synthetic", "H2")
    argv = ["--principal", "workspace-b", "--project", "project-alpha", "--decision-id", "ADR-0101"]
    code, text = raw(decisions, *argv)
    everything = text + capsys.readouterr().err
    result = json.loads(text)
    assert code == 0 and result["records"] == [] and result["superseded"] == []
    assert hidden not in everything and "H2" not in everything and "ADR-0102" not in everything
    code, wide = run(decisions, "--principal", "workspace-a", *argv[2:])
    assert wide["superseded"] == [{"asked": "ADR-0101", "by": [hidden]}]


# Determinism, queries and text -----------------------------------------------------------


def test_the_same_input_and_generation_give_the_same_bytes(corpus):
    env, scenario = corpus
    for task in scenario.tasks[:6]:
        first = raw(env, *task_args(task, "--include-proposed"))
        second = raw(env, *task_args(task, "--include-proposed"))
        assert first[0] == second[0]
        # `--task` adds to the task total, the one part that moves: the second call starts
        # where the first ended. Without it, the bytes are the same.
        before, after = json.loads(first[1]), json.loads(second[1])
        assert after["budget"]["task"]["used_before"] == before["budget"]["task"]["used_after"]
        before["budget"]["task"] = after["budget"]["task"] = None
        assert before == after
        argv = [part for part in task_args(task, "--include-proposed") if part != task["task_id"]]
        argv.remove("--task")
        assert raw(env, *argv) == raw(env, *argv)


def test_fts5_syntax_in_the_query_is_only_words(corpus):
    env, _ = corpus
    base = ["--principal", kc.PRINCIPAL_ALL, "--project", "project-alpha", "--component", "queue"]
    for hostile in ['"保持期間" OR NEAR(a b)', 'summary:x * ^ "', "') -- AND NOT 保持期間", '"""']:
        code, result = run(env, *base, "--query", hostile)
        assert code == 0, hostile
        plain = " ".join(terms.terms(hostile))
        assert run(env, *base, "--query", plain)[1] == result


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, inner in value.items():
            yield key
            yield from strings(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from strings(inner)


def test_control_and_bidi_characters_do_not_reach_the_output(home, tmp_path):
    write_config(home)
    hostile = item(
        "C1",
        summary="保持期間\x1b[2J は\u202e 14 日\u2066",
        applies_when=["\x07キューを作る"],
        exceptions=["\x9b例外"],
    )
    update(home, write_input(tmp_path / "in.json", [hostile]))
    code, text = raw(home, "--principal", "workspace-a", "--project", "project-alpha")
    value = json.loads(text)
    assert code == 0 and value["records"]
    for found in strings(value):
        assert not markdown._CONTROL.search(found), found
    assert "\u202e" not in text and "\x1b" not in text and "\\u001b" not in text


# Status values ---------------------------------------------------------------------------


def test_a_config_the_index_has_not_followed_is_a_stale_index(home, tmp_path):
    write_config(home)
    path = write_input(tmp_path / "in.json", [item("R1")])
    update(home, path)
    code, result = run(home, "--principal", "workspace-a", "--project", "project-alpha")
    assert code == 0 and result["status"] == "ok" and result["records"]
    changed = json.loads(json.dumps(CONFIG))
    changed["principals"]["workspace-c"] = {
        "readers": ["team-alpha"],
        "classifications": ["internal"],
    }
    write_config(home, changed)
    code, result = run(home, "--principal", "workspace-a", "--project", "project-alpha")
    assert code == 1 and result["status"] == "stale_index"
    assert result["records"] == [] and result["mandatory"]["complete"] is False
    update(home, path)
    code, result = run(home, "--principal", "workspace-a", "--project", "project-alpha")
    assert code == 0 and result["status"] == "ok"


def test_freshness_follows_the_clock(corpus, clock):
    env, scenario = corpus
    task = scenario.tasks[1]
    _, fresh = run(env, *task_args(task))
    assert fresh["freshness"]["stale"] == 0 and fresh["freshness"]["unknown"] == 0
    assert {v["freshness"] for v in fresh["records"]} == {"fresh"}
    clock["now"] = "2026-12-10T00:00:00Z"
    _, stale = run(env, *task_args(task))
    assert stale["freshness"]["stale"] == len(stale["records"]) + len(stale["proposed"]) > 0
    _, loose = run(env, *task_args(task, "--stale-after-hours", "10000"))
    assert loose["freshness"]["stale"] == 0
    clock["now"] = "2026-11-26T00:00:00Z"
    _, early = run(env, *task_args(task))
    assert early["freshness"]["unknown"] == len(early["records"])


def test_the_approved_conflict_is_reported(corpus):
    env, scenario = corpus
    pair = set(scenario.traps["approved_conflict"])
    argv = ["--principal", kc.PRINCIPAL_ALL, "--project", "project-beta", "--component", "batch"]
    code, result = run(env, *argv, "--max-tokens", "100000")
    assert code == 0
    assert any(pair <= set(c["ids"]) for c in result["conflicts"])
    assert all(c["reason"] == "both_approved_same_subject" for c in result["conflicts"])


def test_include_proposed_returns_them_apart(corpus):
    env, scenario = corpus
    statuses = {key: value["status"] for key, value in by_id(scenario).items()}
    argv = ["--principal", kc.PRINCIPAL_ALL, "--project", "project-alpha", "--max-tokens", "100000"]
    _, plain = run(env, *argv)
    _, asked = run(env, *argv, "--include-proposed")
    assert plain["proposed"] == [] and asked["proposed"]
    assert {statuses[v["id"]] for v in asked["proposed"]} == {"proposed"}
    assert {statuses[v["id"]] for v in asked["records"]} == {"approved"}
    assert asked["records"] == plain["records"] and asked["mandatory"] == plain["mandatory"]


@pytest.mark.parametrize(
    "argv",
    [
        ["--component", "queue"],
        ["--project", "project-alpha", "--max-tokens", "0"],
        ["--project", "project-alpha", "--decision-id", "x" * 65],
        ["--project", "project-alpha", "--stale-after-hours", "-1"],
    ],
)
def test_bad_arguments_exit_2(corpus, argv):
    env, _ = corpus
    code, text = raw(env, "--principal", kc.PRINCIPAL_ALL, *argv)
    assert code == 2 and text == ""


def test_no_index_yet_exits_2(home):
    write_config(home)
    code, text = raw(home, "--principal", "workspace-a")
    assert code == 2 and text == ""


def test_the_search_module_is_what_the_cli_runs(corpus):
    env, scenario = corpus
    task = scenario.tasks[2]
    _, printed = run(env, *task_args(task))
    request = search.Request(
        project=task["project_id"],
        component=task["component_id"],
        task=task["task_id"],
        query=task["query"],
        paths=tuple(task["paths"]),
        resource_types=tuple(task["resource_types"]),
    )
    direct = search.run(paths.state_root(env), task["principal"], request, SEARCHED)
    # The CLI adds the task total (`--task`) around what the search module returns.
    assert printed["budget"]["task"]["used_after"] == printed["budget"]["used_tokens"]
    printed["budget"]["task"] = None
    assert direct == printed
