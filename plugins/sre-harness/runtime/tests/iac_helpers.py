"""Helpers for the IaC tests: the opa fixture and the `iac validate` command line."""

import json
import os
import shutil
from pathlib import Path

import pytest

from tests.helpers import run_cli, run_dir

V2_SCHEMA = (
    Path(__file__).resolve().parents[3]
    / "toshipon-mode/skills/autonomous-improvement/schemas/evaluation.schema.json"
)
OPA_SKIP_REASON = (
    "opa is not on PATH: the Rego risk classification runs only with the opa binary "
    "(CI installs a pinned, checksum-verified release). Without it `iac validate` records "
    "the risk as unevaluable."
)


@pytest.fixture
def opa_on_path():
    """Skip when opa is missing; fail instead when SRE_HARNESS_REQUIRE_OPA=1 (CI sets it)."""
    if shutil.which("opa") is None:
        if os.environ.get("SRE_HARNESS_REQUIRE_OPA") == "1":
            pytest.fail("SRE_HARNESS_REQUIRE_OPA=1 but opa is not on PATH.")
        pytest.skip(OPA_SKIP_REASON)


@pytest.fixture
def no_opa(monkeypatch, tmp_path):
    """A PATH on which opa cannot be found, whatever the machine has installed."""
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))


def validate(env, case=None, *extra, plan=None, evidence=None, delivery=None):
    """Run `iac validate` through cli.main. Returns (exit code, parsed stdout or None)."""
    plan = plan if plan is not None else case["plan"]
    evidence = evidence if evidence is not None else case["evidence"]
    argv = ["iac", "validate", "--plan", str(plan), "--evidence", str(evidence)]
    if delivery is None and case is not None:
        delivery = case.get("delivery")
    if delivery is not None:
        argv += ["--delivery", str(delivery)]
    return run_cli([*argv, *extra], env)


def read(env, run_id, name):
    return json.loads((run_dir(env, run_id) / name).read_text(encoding="utf-8"))


def runs(env):
    root = Path(env["AI_HARNESS_HOME"]) / "sre-harness" / "runs"
    return sorted(path.name for path in root.iterdir()) if root.is_dir() else []
