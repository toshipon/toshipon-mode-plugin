"""Structure checks: where a process or a socket could come from, and what the repo carries.

The process and network check is a guard against writing such code by mistake. It reads the
syntax tree of the package; it is not a sandbox and does not stop code written to evade it.
"""

import ast
import json
import re
from pathlib import Path

import pytest

from sre_harness import privacy

RUNTIME = Path(__file__).resolve().parents[2]
PLUGIN = RUNTIME.parent
REPO = PLUGIN.parents[1]
PACKAGE = RUNTIME / "sre_harness"

PROCESS_AND_NETWORK = {
    "subprocess",
    "multiprocessing",
    "socket",
    "ssl",
    "urllib",
    "http",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "asyncio",
    "pty",
    "ctypes",
    "requests",
    "httpx",
    "aiohttp",
    "webbrowser",
    "posix",
    "_posixsubprocess",
    "importlib",
    "concurrent",
    "runpy",
}
OS_MODULES = {"os", "posix"}
OS_CALLS = re.compile(r"system|popen|exec[lv]p?e?|spawn[lv]p?e?|posix_spawnp?|fork|forkpty|kill")
DYNAMIC = {"eval", "exec", "compile", "__import__"}
ALLOWED_HOSTS = {
    "example.com",
    "example.org",
    "github.com",
    "api.github.com",
    "json-schema.org",
    "json.schemastore.org",
}


def modules():
    return sorted(path for path in PACKAGE.rglob("*.py") if path.name != "guard.py")


def guard_internals():
    """Names guard binds by import (subprocess, os, ...). Reachable through guard, not its API."""
    tree = ast.parse((PACKAGE / "guard.py").read_text(encoding="utf-8"))
    return {
        (alias.asname or alias.name).split(".")[0]
        for node in tree.body
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }


GUARD_INTERNALS = guard_internals()


def _outside_guard_api(name):
    return name.startswith("_") or name in GUARD_INTERNALS


def _imports(tree, offenders):
    """Record offending imports. Returns the names bound to os and to the guard module."""
    os_names, guard_names = set(), set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in PROCESS_AND_NETWORK:
                    offenders.append(f"import {alias.name}")
                if alias.name == "os" or (alias.name.startswith("os.") and not alias.asname):
                    os_names.add(alias.asname or "os")
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            module = node.module or ""
            if module.split(".")[0] in PROCESS_AND_NETWORK:
                offenders.append(f"import {module}")
            elif module in OS_MODULES:
                offenders.append(f"from {module} import ...")
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if (node.module or "").split(".")[-1] == "guard" and _outside_guard_api(alias.name):
                    offenders.append(f"from guard import {alias.name}")
                if node.module is None and alias.name == "guard":
                    guard_names.add(alias.asname or "guard")
    return os_names, guard_names


def _is_safe_os_getattr(call):
    """getattr(os, "O_NOFOLLOW", 0) reads a constant; any other name could be a process call."""
    name = call.args[1] if len(call.args) > 1 else None
    return (
        isinstance(name, ast.Constant)
        and isinstance(name.value, str)
        and not OS_CALLS.fullmatch(name.value)
    )


def find_offenders(source):
    """Ways a module could start a process or open a socket outside guard, as short strings."""
    tree = ast.parse(source)
    offenders = []
    os_names, guard_names = _imports(tree, offenders)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            owner = node.value.id
            if owner in os_names and OS_CALLS.fullmatch(node.attr):
                offenders.append(f"os.{node.attr}")
            if owner in guard_names and _outside_guard_api(node.attr):
                offenders.append(f"guard.{node.attr}")
        if isinstance(node, ast.Attribute) and node.attr == "_run":
            offenders.append("reaches into guard._run")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in DYNAMIC:
                offenders.append(f"{node.func.id}()")
            first = node.args[0] if node.args else None
            if node.func.id == "getattr" and isinstance(first, ast.Name):
                if first.id in os_names and not _is_safe_os_getattr(node):
                    offenders.append("getattr(os, ...)")
    return offenders


def test_only_guard_can_start_a_process_or_open_a_socket():
    offenders = [
        f"{path.relative_to(RUNTIME)}: {offender}"
        for path in modules()
        for offender in find_offenders(path.read_text(encoding="utf-8"))
    ]
    assert offenders == []


# Each line is one way to reach a process or the network without going through guard.
OFFENDING_SOURCES = [
    "import subprocess",
    "import socket",
    "from urllib import request",
    "import http.client",
    "import multiprocessing.pool",
    "import os\nos.system('x')",
    "import os\nos.posix_spawn('x', [], {})",
    "import os\nos.fork()",
    "from os import system",
    "from os import popen as p",
    "from posix import system",
    "import posix",
    "import os as o\no.system('x')",
    "import os as o\no.execv('x', [])",
    "import os\ngetattr(os, 'system')('x')",
    "import os as o\ngetattr(o, 'fork')()",
    "import os\nname = 'system'\ngetattr(os, name)('x')",
    "import importlib",
    "import importlib.util",
    "from importlib import import_module",
    "import concurrent.futures",
    "from concurrent.futures import ProcessPoolExecutor",
    "import runpy",
    "from .guard import subprocess",
    "from .guard import _run as r",
    "from ..guard import _executable",
    "from .guard import shutil",
    "from . import guard\nguard._run([])",
    "from .. import guard as g\ng.subprocess.run([])",
    "eval('1')",
    "exec('1')",
    "compile('1', 'x', 'exec')",
    "__import__('subprocess')",
]

# Uses the package relies on. They must stay clean or the check is too broad to keep.
CLEAN_SOURCES = [
    "import os\nos.environ.get('X')",
    "import os\nflags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)",
    "import os\nos.open('x', os.O_RDONLY)",
    "from os.path import join",
    "from . import guard\nguard.git('/x', ['rev-parse'])",
    "from .. import guard, paths\nguard.gh_rest('pulls')",
    "from .guard import gh_rest, git",
    "from .errors import BoundaryError",
]


@pytest.mark.parametrize("source", OFFENDING_SOURCES)
def test_the_check_catches_each_way_around_guard(source):
    assert find_offenders(source) != []


@pytest.mark.parametrize("source", CLEAN_SOURCES)
def test_the_check_leaves_ordinary_code_alone(source):
    assert find_offenders(source) == []


def tracked_text_files():
    skip = {"__pycache__", ".pytest_cache", ".ruff_cache"}
    for path in sorted(PLUGIN.rglob("*")):
        if path.is_file() and not skip & set(path.parts) and path.name != ".coverage":
            yield path, path.read_text(encoding="utf-8")


def test_the_plugin_carries_no_secret_shaped_text():
    hits = {
        str(path.relative_to(PLUGIN)): privacy.find_secrets(text)
        for path, text in tracked_text_files()
        if privacy.find_secrets(text) and path.name not in {"privacy.py", "test_privacy.py"}
    }
    assert hits == {}


def test_every_url_points_at_a_public_or_reserved_host():
    hosts = {}
    for path, text in tracked_text_files():
        for host in re.findall(r"https?://(?:[^\s/@]*@)?([A-Za-z0-9.-]+)", text):
            host = host.lower().rstrip(".")
            if host not in ALLOWED_HOSTS and not host.endswith((".invalid", ".example")):
                hosts.setdefault(host, str(path.relative_to(PLUGIN)))
    assert hosts == {}


def test_the_plugin_is_registered_with_the_same_version():
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    market = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    (entry,) = [plugin for plugin in market["plugins"] if plugin["name"] == "sre-harness"]
    assert entry["source"] == "./plugins/sre-harness"
    assert entry["version"] == manifest["version"]
    init = (PACKAGE / "__init__.py").read_text()
    assert f'__version__ = "{manifest["version"]}"' in init


def test_the_workflow_runs_on_changes_to_this_plugin_only():
    workflow = (REPO / ".github" / "workflows" / "sre-harness.yml").read_text()
    assert "plugins/sre-harness/**" in workflow
    assert "contents: read" in workflow
