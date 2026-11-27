"""Reading the project context explorer (another plugin) for the contract tests.

Nothing here imports the explorer. Constants come from its syntax tree; the two pure pieces that
are run (`untrusted.py` and `cited_text`) are compiled from their syntax tree only after every
import in them is checked against ALLOWED_IMPORTS, so nothing of the explorer that reads files or
runs git is executed. The explorer's files are only read.

Without the explorer (sre-harness installed on its own) `require_explorer` skips. In this
repository the marketplace lists the toshipon-mode plugin next to sre-harness, so a missing
explorer is a failure there, and so is SRE_HARNESS_REQUIRE_EXPLORER=1.
"""

import ast
import json
import os
import re
import unicodedata
from pathlib import Path

import pytest

from sre_harness import records

RUNTIME = Path(__file__).resolve().parents[1]
PLUGIN = RUNTIME.parent
REPO = PLUGIN.parents[1]
EXPLORER = REPO / "plugins" / "toshipon-mode" / "skills" / "project-context-explorer"
CODE = EXPLORER / "runtime" / "context_recovery"
CONTRACT = EXPLORER / "references" / "contract.md"
MARKETPLACE = REPO / ".claude-plugin" / "marketplace.json"


def _required():
    if os.environ.get("SRE_HARNESS_REQUIRE_EXPLORER") == "1":
        return True
    try:
        market = json.loads(MARKETPLACE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return any(
        plugin.get("name") == "toshipon-mode" and plugin.get("source") == "./plugins/toshipon-mode"
        for plugin in market.get("plugins", [])
    )


def require_explorer():
    """Skip, or fail where the explorer must be present."""
    if not CODE.is_dir() or not CONTRACT.is_file():
        if _required():
            pytest.fail(
                f"the project context explorer is missing at {EXPLORER}, but this repository's "
                "marketplace carries it (or SRE_HARNESS_REQUIRE_EXPLORER=1)"
            )
        pytest.skip(
            "the project context explorer (toshipon-mode plugin) is not installed next to "
            "sre-harness; the contract check needs both"
        )


# Reading the explorer ------------------------------------------------------------------------


def _tree(name):
    return ast.parse((CODE / name).read_text(encoding="utf-8"))


def _assignment(tree, name):
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return node.value
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Tuple) and name in [e.id for e in target.elts]  # type: ignore[attr-defined]
            for target in node.targets
        ):
            (target,) = node.targets
            position = [e.id for e in target.elts].index(name)  # type: ignore[attr-defined]
            return node.value.elts[position]  # type: ignore[attr-defined]
    raise AssertionError(f"{name} is not assigned at the top of the explorer module")


def constant(module, name):
    """A literal (string, tuple, list) the explorer assigns at module level."""
    return ast.literal_eval(_assignment(_tree(module), name))


def compiled_pattern(module, name):
    """The pattern string of `NAME = re.compile("...")` in an explorer module."""
    value = _assignment(_tree(module), name)
    assert isinstance(value, ast.Call) and ast.unparse(value.func) == "re.compile"
    return ast.literal_eval(value.args[0])


def function(module, name):
    for node in _tree(module).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{module} has no function {name}")


ALLOWED_IMPORTS = {("import", "re"), ("import", "unicodedata"), ("from .base", "sha256")}


def run_pure(module, names, provided):
    """The functions and constants `names` of an explorer module whose only imports are in
    ALLOWED_IMPORTS, compiled from its syntax tree without those imports. `provided` stands in
    for what `.base` gave it."""
    tree = _tree(module)
    body = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            assert {("import", alias.name) for alias in node.names} <= ALLOWED_IMPORTS
        elif isinstance(node, ast.ImportFrom):
            found = {(f"from {'.' * node.level}{node.module}", a.name) for a in node.names}
            assert found <= ALLOWED_IMPORTS, found
        else:
            body.append(node)
    space = {"re": re, "unicodedata": unicodedata, **provided}
    code = compile(ast.Module(body=body, type_ignores=[]), str(CODE / module), "exec")
    exec(code, space)  # noqa: S102 - the checked, pure part of the explorer module, in a test
    return {name: space[name] for name in names}


def plain_sha256():
    """The explorer's base.sha256 is sha256 of the UTF-8 text, hex: the harness's own."""
    node = function("base.py", "sha256")
    expected = ast.parse(
        "def sha256(text):\n"
        "    data = text if isinstance(text, bytes) else text.encode('utf-8')\n"
        "    return hashlib.sha256(data).hexdigest()\n"
    ).body[0]
    assert ast.dump(node) == ast.dump(expected)
    return records.sha256


def explorer_untrusted():
    return run_pure(
        "untrusted.py",
        (
            "_INSTRUCTION_SHAPES",
            "_LABEL",
            "OPEN",
            "CLOSE",
            "PREAMBLE",
            "invisible",
            "clean",
            "one_line",
            "instruction_like",
            "envelope",
        ),
        {"sha256": plain_sha256()},
    )


def explorer_cited_text():
    node = function("claims.py", "cited_text")
    names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert names <= {"text", "where", "lines", "len", "None"}, names  # reads its arguments only
    space = {}
    module = ast.Module(body=[node], type_ignores=[])
    exec(compile(module, str(CODE / "claims.py"), "exec"), space)  # noqa: S102 - pure, checked
    return space["cited_text"]


def contract_text():
    return CONTRACT.read_text(encoding="utf-8")


def contract_section(title):
    text = contract_text()
    start = text.index(f"## {title}\n")
    following = text.find("\n## ", start + 1)
    return text[start : following if following != -1 else len(text)]


def explorer_accepts(locator, revision):
    """The explorer's parse_locator and check_reference, by their patterns: (kind, path,
    start, end) when the reference is well formed, else None."""
    if not isinstance(locator, str) or not isinstance(revision, str):
        return None
    git = re.compile(compiled_pattern("claims.py", "_GIT"))
    pr = re.compile(compiled_pattern("claims.py", "_PR"))
    doc = re.compile(compiled_pattern("claims.py", "_DOC"))
    doc_revision = re.compile(compiled_pattern("claims.py", "_DOC_REVISION"))
    commit = re.compile(compiled_pattern("gitread.py", "REVISION"))
    match = git.match(locator)
    if match:
        path = match["path"]
        parts = path.split("/")
        explorer_invisible = explorer_untrusted()["invisible"]
        if (
            path.startswith("/")
            or ".." in parts
            or "" in parts
            or any(map(explorer_invisible, path))
            or path.startswith("-")
            or ":" in path
        ):
            return None  # gitread.checked_path
        start = int(match["start"]) if match["start"] else None
        end = int(match["end"]) if match["end"] else start
        if start is not None and not 1 <= start <= (end or start):
            return None
        return ("git", path, start, end) if commit.match(revision) else None
    if pr.match(locator):
        return ("pr", None, None, None) if commit.match(revision) else None
    if doc.match(locator):
        return ("doc", None, None, None) if doc_revision.match(revision) else None
    return None


# The explorer's entity contract (index.check_entity), from its constants ----------------------

ENTITY_NAMES = ("_ID", "_ENTITY_KEYS", "TYPES", "RELATIONS", "DECISION_STATUSES", "MAX_DESCRIPTION")


def entity_problems(entity):
    """What the explorer's `index add` would refuse in one entity, by the rules of
    index.check_entity read from its constants. Empty when `index add` stores it. The check
    itself is not run: it imports the explorer's git reader."""
    node = function("index.py", "check_entity")
    used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
    assert set(ENTITY_NAMES) <= used, "check_entity no longer uses the constants read here"
    identifier = re.compile(compiled_pattern("index.py", "_ID"))
    keys = constant("index.py", "_ENTITY_KEYS")
    types = constant("index.py", "TYPES")
    relations = constant("index.py", "RELATIONS")
    statuses = constant("index.py", "DECISION_STATUSES")
    longest = constant("index.py", "MAX_DESCRIPTION")
    if not isinstance(entity, dict) or not identifier.match(str(entity.get("id"))):
        return ["id"]
    problems = [f"unknown field {key}" for key in sorted(set(entity) - set(keys))]
    if entity.get("type") not in types:
        problems.append("type")
    description = entity.get("description")
    if (
        not isinstance(description, str)
        or not description.strip()
        or len(description) > longest
        or "\n" in description.strip()
    ):
        problems.append("description")
    source = entity.get("source")
    if source is not None:
        if not isinstance(source, dict) or set(source) - {"locator", "revision", "sha256", "size"}:
            problems.append("source shape")
        elif explorer_accepts(source.get("locator"), source.get("revision", "a" * 40)) is None:
            problems.append("source reference")
    for relation in entity.get("relationships", []):
        if (
            not isinstance(relation, dict)
            or set(relation) != {"type", "to"}
            or relation["type"] not in relations
            or not identifier.match(str(relation["to"]))
        ):
            problems.append("relationship")
    if "status" in entity and entity["status"] not in statuses:
        problems.append("status")
    return problems
