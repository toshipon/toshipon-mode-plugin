"""Structure checks: where a process or a socket could come from, and what the repo carries."""

import ast
import json
import re
from pathlib import Path

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
}
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


def imported(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            yield node.module or ""


def test_only_guard_can_start_a_process_or_open_a_socket():
    offenders = []
    for path in modules():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for name in imported(tree):
            if name.split(".")[0] in PROCESS_AND_NETWORK or name == "importlib":
                offenders.append(f"{path.relative_to(RUNTIME)}: import {name}")
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and OS_CALLS.fullmatch(node.attr):
                if isinstance(node.value, ast.Name) and node.value.id == "os":
                    offenders.append(f"{path.relative_to(RUNTIME)}: os.{node.attr}")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in DYNAMIC:
                    offenders.append(f"{path.relative_to(RUNTIME)}: {node.func.id}()")
            if isinstance(node, ast.Attribute) and node.attr == "_run":
                offenders.append(f"{path.relative_to(RUNTIME)}: reaches into guard._run")
    assert offenders == []


def test_the_check_would_catch_an_offender():
    tree = ast.parse("import os\nfrom urllib import request\nos.system('x')\n")
    assert "urllib" in set(imported(tree))
    assert any(isinstance(node, ast.Attribute) and node.attr == "system" for node in ast.walk(tree))


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
