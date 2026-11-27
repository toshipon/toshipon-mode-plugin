"""Helpers shared by the knowledge tests. Everything here is synthetic."""

import copy
import json
from pathlib import Path

from sre_harness import paths, records
from tests.helpers import run_cli

UPDATED_AT = "2026-11-01T00:00:00Z"
APPROVAL = {"approver": "alice", "decided_at": "2026-10-01T00:00:00Z", "via": "review approve"}
SHA = "0" * 64

CONFIG = {
    "schema_version": 1,
    "projects": {
        "project-alpha": {
            "repos": ["example-org/alpha"],
            "default_access": {"classification": "internal", "readers": ["team-alpha"]},
            "components": {"queue": {"path_globs": ["infra/queue/**"]}},
        },
        "project-gamma": {
            "repos": ["example-org/gamma"],
            "default_access": {"classification": "confidential", "readers": ["team-gamma"]},
            "components": {},
        },
    },
    "principals": {
        "workspace-a": {
            "readers": ["everyone", "team-alpha", "team-gamma"],
            "classifications": ["public", "synthetic", "internal", "confidential"],
        },
        "workspace-b": {
            "readers": ["everyone", "team-alpha"],
            "classifications": ["public", "synthetic", "internal"],
        },
    },
}


def item(key, **changes):
    """An approved, advisory, project-level item of project-alpha readable by team-alpha."""
    value = {
        "origin": {"kind": "synthetic", "key": key},
        "kind": "rule",
        "scope": {
            "level": "project",
            "project_id": "project-alpha",
            "component_id": None,
            "task_id": None,
        },
        "facets": {"topic": ["retention"], "resource_type": ["queue"]},
        "access": {"classification": "internal", "readers": ["team-alpha"]},
        "summary": f"{key}: キューのメッセージ保持期間は 14 日にする",
        "applies_when": ["キューを新しく作る"],
        "exceptions": [],
        "status": "approved",
        "enforcement": "advisory",
        "evidence": [
            {"locator": f"doc:synthetic:{key}", "revision": "r1", "content_sha256": SHA, "size": 10}
        ],
        "updated_at": UPDATED_AT,
        "approval": copy.deepcopy(APPROVAL),
    }
    value.update(changes)
    return value


def source_file(items, *, source="synthetic", complete=True, tombstones=None):
    value = {"schema_version": 1, "source": source, "complete": complete, "items": items}
    if tombstones is not None:
        value["tombstones"] = tombstones
    return value


def write_input(path, items, **options):
    path = Path(path)
    path.write_text(json.dumps(source_file(items, **options), ensure_ascii=False))
    return path


def state_root(env):
    return paths.state_root(env)


def knowledge_dir(env):
    return Path(env["AI_HARNESS_HOME"]) / "sre-harness" / "knowledge"


def write_config(env, value=None):
    directory = knowledge_dir(env)
    paths.write_private(directory / "config.json", records.canonical(value or CONFIG))
    return directory / "config.json"


def update(env, input_path, *extra):
    return run_cli(
        ["knowledge", "update", "--source", "file", "--input", str(input_path), *extra], env
    )
