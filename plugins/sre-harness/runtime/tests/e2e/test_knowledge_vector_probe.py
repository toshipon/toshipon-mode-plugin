"""`examples/synthetic/knowledge_vector_probe.py`: the experiment with vectors stays outside the
package. Its optional packages (sqlite-vec, sentence-transformers) and a model are not in CI, so
only this is checked: importing it needs none of them, and without them it says what is missing
and exits 2."""

import ast
import importlib.util
import json

from examples.synthetic import knowledge_vector_probe as probe
from sre_harness.knowledge import store
from tests.invariants.test_boundaries import PACKAGE, RUNTIME
from tests.invariants.test_knowledge_boundaries import sql_imports

PROBE = RUNTIME / "examples" / "synthetic" / "knowledge_vector_probe.py"
HEAVY = {"sqlite_vec", "sentence_transformers", "numpy", "torch", "transformers"}


def _top_level_imports(path):
    found = set()
    for node in ast.parse(path.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.Import):
            found |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            found.add((node.module or "").split(".")[0])
    return found


def test_importing_the_probe_needs_no_optional_package():
    assert not _top_level_imports(PROBE) & HEAVY
    assert set(probe.OPTIONAL) <= HEAVY


def test_without_the_optional_packages_it_names_them_and_exits_2(monkeypatch, capsys):
    real = importlib.util.find_spec

    def find_spec(name, *args, **kwargs):
        return None if name in probe.OPTIONAL else real(name, *args, **kwargs)

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    monkeypatch.setattr(store, "extension_loading", lambda: True)
    assert probe.main(["--model", "a-local-model"]) == 2
    out, err = capsys.readouterr()
    value = json.loads(out)
    assert value["status"] == "missing" and len(value["missing"]) == 2
    assert "sqlite-vec" in err and "sentence-transformers" in err


def test_without_extension_loading_or_a_model_it_says_so(monkeypatch, capsys):
    monkeypatch.setattr(store, "extension_loading", lambda: False)
    assert probe.main([]) == 2
    err = capsys.readouterr().err
    assert "cannot load extensions" in err and "--model" in err


def test_the_probe_lives_outside_the_package_where_sqlite3_may_be_imported():
    assert PACKAGE not in PROBE.parents
    assert PROBE not in set(PACKAGE.rglob("*.py"))
    assert sql_imports(PROBE.read_text(encoding="utf-8")) == ["sqlite3"]
