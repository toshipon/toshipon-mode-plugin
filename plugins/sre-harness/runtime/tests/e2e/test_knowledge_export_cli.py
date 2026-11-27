"""`sre-harness knowledge export --format context-entities` end to end, through `cli.main`.

The output is what the project context explorer's `index add --file` reads: a JSON list of
entities. Each one is checked against the explorer's entity contract (tests/explorer_helpers.py
reads it from the explorer's files). The export goes through the same access filter as search:
a record the principal cannot read is nowhere in it.
"""

import io
import json

import pytest

from examples.synthetic import knowledge_corpus as kc
from sre_harness import cli, paths
from sre_harness.knowledge import ingest, model, store
from sre_harness.knowledge.adapters import build
from tests.explorer_helpers import entity_problems, require_explorer
from tests.knowledge_helpers import (
    CONFIG,
    evidence,
    git_evidence,
    item,
    knowledge_dir,
    update,
    write_config,
    write_input,
)

INDEXED = "2026-11-27T00:00:00Z"
FORMAT = ["--format", "context-entities"]


@pytest.fixture(scope="module")
def corpus(tmp_path_factory):
    home = tmp_path_factory.mktemp("export")
    env = {"AI_HARNESS_HOME": str(home / "state")}
    root = paths.state_root(env)
    scenario = kc.scenario(50)
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    path = home / "corpus.json"
    path.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    ingest.update(root, build("file", {"input": path}), now=INDEXED)
    by_id = {
        model.record_id(i["origin"]["kind"], i["origin"]["key"]): i
        for i in scenario.corpus["items"]
    }
    return env, scenario, by_id


def export(env, *argv):
    """(exit code, stdout text, parsed stdout or None)."""
    out = io.StringIO()
    code = cli.main(["knowledge", "export", *argv], env=env, out=out)
    text = out.getvalue()
    return code, text, (json.loads(text) if text.strip() else None)


def _readable(record_item, principal):
    access = record_item.get("access")
    return (
        access is not None
        and access["classification"] in principal["classifications"]
        and bool(set(access["readers"]) & set(principal["readers"]))
    )


def test_every_entity_follows_the_explorers_contract(corpus):
    require_explorer()
    env, _, _ = corpus
    code, _, entities = export(env, "--principal", kc.PRINCIPAL_ALL, *FORMAT)
    assert code == 0
    assert isinstance(entities, list) and entities
    assert {e["id"]: entity_problems(e) for e in entities if entity_problems(e)} == {}
    assert [e["id"] for e in entities] == sorted(e["id"] for e in entities)
    assert {e["status"] for e in entities} == {"approved"}


def test_only_approved_records_the_principal_reads_are_exported(corpus):
    env, scenario, by_id = corpus
    code, _, entities = export(env, "--principal", kc.PRINCIPAL_LIMITED, *FORMAT)
    assert code == 0
    exported = {e["id"] for e in entities}
    holder = scenario.config["principals"][kc.PRINCIPAL_LIMITED]
    for record_id in exported:
        source = by_id[record_id]
        assert source["status"] == "approved"
        assert _readable(source, holder)
        assert source["scope"]["project_id"] != kc.THIRD_PROJECT
    applicable = {
        i
        for task in scenario.tasks
        if task["principal"] == kc.PRINCIPAL_LIMITED
        for i in task["applicable"]
    }
    assert applicable <= exported
    gamma = {i for i, source in by_id.items() if source["scope"]["project_id"] == kc.THIRD_PROJECT}
    assert gamma and not gamma & exported
    rejected = {
        model.record_id(kc.ORIGIN, key)
        for keys in scenario.traps["rejected"].values()
        for key in keys
    }
    assert not rejected & exported


def test_a_replaced_record_is_not_exported_and_the_summary_is_the_description(corpus):
    env, scenario, by_id = corpus
    _, _, entities = export(env, "--principal", kc.PRINCIPAL_ALL, *FORMAT)
    exported = {e["id"]: e for e in entities}
    first, second, last = scenario.traps["supersession_chain"]
    assert last in exported and first not in exported and second not in exported
    assert exported[last]["description"] == by_id[last]["summary"]
    for entity in entities:
        kind = by_id[entity["id"]]["kind"]
        assert entity["type"] == ("decision" if kind == "decision" else "requirement")


def test_project_keeps_the_organization_and_that_project(corpus):
    env, _, by_id = corpus
    code, _, entities = export(
        env, "--principal", kc.PRINCIPAL_ALL, "--project", "project-beta", *FORMAT
    )
    assert code == 0
    scopes = {
        (by_id[e["id"]]["scope"]["level"], by_id[e["id"]]["scope"]["project_id"]) for e in entities
    }
    assert {project for _, project in scopes} == {None, "project-beta"}
    assert ("organization", None) in scopes


def test_the_same_index_gives_the_same_bytes_and_nothing_is_written(corpus):
    env, _, _ = corpus
    folder = knowledge_dir(env)
    before = sorted((p.name, p.stat().st_size) for p in folder.iterdir())
    first = export(env, "--principal", kc.PRINCIPAL_ALL, *FORMAT)[1]
    second = export(env, "--principal", kc.PRINCIPAL_ALL, *FORMAT)[1]
    assert first == second
    assert sorted((p.name, p.stat().st_size) for p in folder.iterdir()) == before
    assert not (folder / "tasks").exists()


def test_git_evidence_becomes_the_entity_source_and_other_locators_do_not(home, tmp_path):
    require_explorer()
    write_config(home)
    sha = "1" * 40
    git = git_evidence("infra/queue/main.tf", sha, "retention = 14\n", start=1)
    registry = evidence("registry:RG-0001/v0001.md", "# rule\n")
    doc = evidence("doc:synthetic:K3", "text", revision="r1")
    items = [
        item("K1", evidence=[registry, git]),
        item("K2", evidence=[registry]),
        item("K3", evidence=[doc]),
    ]
    code, _ = update(home, write_input(tmp_path / "in.json", items))
    assert code == 0
    code, _, entities = export(home, "--principal", "workspace-a", *FORMAT)
    assert code == 0
    by_key = {e["description"].split(":")[0]: e for e in entities}
    assert by_key["K1"]["source"] == {
        "locator": git["locator"],
        "revision": sha,
        "sha256": git["content_sha256"],
        "size": git["size"],
    }
    assert "source" not in by_key["K2"] and "source" not in by_key["K3"]
    assert [entity_problems(e) for e in entities] == [[], [], []]


def test_relationships_are_kept_between_exported_entities_only(home, tmp_path):
    write_config(home)
    target = {"kind": "synthetic", "key": "K2"}
    hidden = {"kind": "synthetic", "key": "K9"}
    items = [
        item(
            "K1",
            relations=[
                {"relation": "refines", "target": target},
                {"relation": "depends_on", "target": hidden},
            ],
        ),
        item("K2"),
        item("K9", access={"classification": "confidential", "readers": ["team-gamma"]}),
    ]
    assert update(home, write_input(tmp_path / "in.json", items))[0] == 0
    _, _, entities = export(home, "--principal", "workspace-b", *FORMAT)
    by_id = {e["id"]: e for e in entities}
    k1, k2, k9 = (model.record_id("synthetic", key) for key in ("K1", "K2", "K9"))
    assert k9 not in by_id
    assert by_id[k1]["relationships"] == [{"type": "refines", "to": k2}]
    assert by_id[k2]["relationships"] == []
    _, _, everything = export(home, "--principal", "workspace-a", *FORMAT)
    wide = {e["id"]: e for e in everything}
    assert wide[k1]["relationships"] == [
        {"type": "depends_on", "to": k9},
        {"type": "refines", "to": k2},
    ]


def test_an_unknown_principal_is_refused_and_names_nobody(corpus, capsys):
    env, _, _ = corpus
    code, text, _ = export(env, "--principal", "nobody", *FORMAT)
    assert (code, text) == (3, "")
    assert kc.PRINCIPAL_ALL not in capsys.readouterr().err


def test_an_index_behind_the_config_exports_nothing(home, tmp_path):
    write_config(home)
    assert update(home, write_input(tmp_path / "in.json", [item("K1")]))[0] == 0
    changed = json.loads(json.dumps(CONFIG))
    changed["principals"]["workspace-c"] = {
        "readers": ["team-alpha"],
        "classifications": ["internal"],
    }
    write_config(home, changed)
    code, text, _ = export(home, "--principal", "workspace-a", *FORMAT)
    assert (code, text) == (1, "")


@pytest.mark.parametrize(
    "argv",
    [
        ["--principal", "workspace-a"],
        ["--principal", "workspace-a", "--format", "entities"],
        ["--principal", "workspace-a", "--project", "../x", *FORMAT],
    ],
)
def test_wrong_arguments_are_input_errors(home, tmp_path, argv):
    write_config(home)
    assert update(home, write_input(tmp_path / "in.json", [item("K1")]))[0] == 0
    assert export(home, *argv)[:2] == (2, "")


def test_no_index_is_an_input_error(home):
    write_config(home)
    assert not (knowledge_dir(home) / store.INDEX).exists()
    assert export(home, "--principal", "workspace-a", *FORMAT)[:2] == (2, "")
