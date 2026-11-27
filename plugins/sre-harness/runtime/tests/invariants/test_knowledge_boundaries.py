"""Structure checks for the knowledge index.

SQL stays in one module, and in that module every read of `records` on behalf of a principal
goes through the one access filter. Like test_boundaries.py, this reads syntax trees; it is a
guard against writing such code by mistake, not a sandbox.
"""

import ast
import json

import pytest

from examples.synthetic import knowledge_corpus
from sre_harness import paths
from sre_harness.knowledge import config, ingest, model, store
from sre_harness.knowledge.adapters import build
from tests.invariants.test_boundaries import PACKAGE, RUNTIME

STORE = PACKAGE / "knowledge" / "store.py"
SQL_MODULES = {"sqlite3", "_sqlite3"}
KNOWLEDGE = PACKAGE / "knowledge"


def sql_imports(source):
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names if alias.name.split(".")[0] in SQL_MODULES]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] in SQL_MODULES:
                found.append(node.module)
    return found


def test_only_the_store_imports_sqlite3():
    offenders = {
        str(path.relative_to(RUNTIME)): sql_imports(path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE.rglob("*.py"))
        if path != STORE and sql_imports(path.read_text(encoding="utf-8"))
    }
    assert offenders == {}
    assert sql_imports(STORE.read_text(encoding="utf-8")) == ["sqlite3"]


@pytest.mark.parametrize(
    "source",
    [
        "import sqlite3",
        "import sqlite3 as db",
        "from sqlite3 import connect",
        "import _sqlite3",
        "from sqlite3.dbapi2 import connect",
    ],
)
def test_the_sql_check_catches_each_import(source):
    assert sql_imports(source) != []


def test_the_sql_check_leaves_other_imports_alone():
    assert sql_imports("import json\nfrom . import store\nfrom .store import reader") == []


def _reader_methods():
    tree = ast.parse(STORE.read_text(encoding="utf-8"))
    (reader,) = [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Reader"]
    return [n for n in reader.body if isinstance(n, ast.FunctionDef)]


def _reads_records(function):
    texts = [
        node.value
        for node in ast.walk(function)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return any("FROM records" in text or "record_terms" in text for text in texts)


def _calls(function, name):
    return any(
        isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
        for node in ast.walk(function)
    )


# Readers that serve the operator only: they count rows or plan a write, and print no record.
OPERATOR_VIEWS = {"status_counts", "plan"}


def test_every_reader_method_that_reads_records_passes_the_access_filter():
    checked = []
    for function in _reader_methods():
        if function.name.startswith("_") or function.name in OPERATOR_VIEWS:
            continue
        if _reads_records(function):
            checked.append(function.name)
            assert _calls(function, "access_clause"), function.name
            params = [a.arg for a in function.args.args]
            assert params[:2] == ["self", "principal"], function.name
    assert {"records", "matching", "provenance", "edges"} <= set(checked)


def test_the_operator_views_print_no_record_content():
    for function in _reader_methods():
        if function.name == "status_counts":
            texts = [
                n.value
                for n in ast.walk(function)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
            ]
            assert any("GROUP BY status" in text for text in texts)
            assert not any("body" in text for text in texts)


def test_the_knowledge_package_calls_no_model_and_no_guard():
    for path in sorted(KNOWLEDGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                names = {alias.name for alias in node.names}
                assert not names & {"llm", "guard"}, path
                assert (node.module or "").split(".")[-1] not in {"llm", "guard"}, path


# Invariants 1-4 over the synthetic corpus ---------------------------------------------------------


@pytest.fixture(scope="module")
def indexed(tmp_path_factory):
    home = tmp_path_factory.mktemp("knowledge")
    env = {"AI_HARNESS_HOME": str(home / "state")}
    root = paths.state_root(env)
    scenario = knowledge_corpus.scenario(300)
    paths.write_private(root / "knowledge" / "config.json", json.dumps(scenario.config))
    corpus = home / "corpus.json"
    corpus.write_text(json.dumps(scenario.corpus, ensure_ascii=False))
    result = ingest.update(root, build("file", {"input": corpus}), now="2026-11-27T00:00:00Z")
    principal = config.principal(scenario.config, knowledge_corpus.PRINCIPAL_ALL)
    with store.reader(root) as reader:
        rows = reader.records(principal)
    return scenario, result, rows


def test_invariant_1_approved_and_superseded_records_carry_an_approval(indexed):
    _, _, rows = indexed
    held = [row for row in rows if row["status"] in {"approved", "superseded"}]
    assert held and all(row["approval"] for row in held)


def test_invariant_2_only_approved_records_are_mandatory(indexed):
    _, _, rows = indexed
    mandatory = [row for row in rows if row["enforcement"] == "mandatory"]
    assert mandatory and {row["status"] for row in mandatory} == {"approved"}


def test_invariant_3_supersedes_has_no_cycle(indexed):
    _, _, rows = indexed
    edges = {row["id"]: row["supersedes"] for row in rows}
    assert any(edges.values())
    assert model.supersession_cycle(edges) is None


def test_invariant_4_every_indexed_record_has_access_with_known_readers(indexed):
    scenario, result, rows = indexed
    known = config.known_readers(scenario.config)
    assert all(set(row["access"]["readers"]) <= known for row in rows)
    rejected = {(r["reason"], r["origin"]["key"]) for r in result["rejected"]}
    traps = scenario.traps["rejected"]
    assert rejected == {(reason, key) for reason, keys in traps.items() for key in keys}
    indexed_keys = {row["origin"]["key"] for row in rows}
    assert not indexed_keys & {key for keys in traps.values() for key in keys}
    assert len(rows) == len(scenario.corpus["items"]) - len(result["rejected"])
