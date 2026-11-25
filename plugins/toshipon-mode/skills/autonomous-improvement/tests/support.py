"""Shared fixtures: a throwaway state home and a synthetic git project."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

from ai_harness import (  # noqa: E402
    agents,
    cli,
    dispatch,
    evaluation,
    evolve,
    loop,
    record,
    redact,
    status,
    store,
)
from ai_harness.jobs import Queue  # noqa: E402
from ai_harness.store import HarnessError  # noqa: E402

__all__ = [
    "HarnessCase",
    "HarnessError",
    "Queue",
    "agents",
    "cli",
    "dispatch",
    "evaluation",
    "evolve",
    "loop",
    "record",
    "redact",
    "status",
    "store",
]

EXAMPLES = ROOT / "examples" / "synthetic"
START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)
CLEAN_AUDIT = {"findings": [], "limitations": ["合成データのみ"]}


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def init_repo(path):
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "synthetic@example.invalid")
    git(path, "config", "user.name", "Synthetic")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")


class HarnessCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="ai-harness-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = store.ensure_home(self.tmp / "state")
        self.project = self.tmp / "project"
        shutil.copytree(EXAMPLES / "project", self.project)
        init_repo(self.project)
        self.spec = json.loads((EXAMPLES / "spec.json").read_text(encoding="utf-8"))
        self.spec["evaluation"]["command"][0] = sys.executable
        self.spec["guard"]["checks"][0]["command"][0] = sys.executable
        self.clock = START
        self.queue = Queue(self.home, store.DEFAULT_CONFIG["queue"])

    def tick(self, seconds=60):
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def settings(self, **changes):
        path = self.project / "settings.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        data.update(changes)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    def read_settings(self):
        return json.loads((self.project / "settings.json").read_text(encoding="utf-8"))

    def start_run(self, **spec_changes):
        self.spec.update(spec_changes)
        run_id = loop.init(self.home, self.project, self.spec, self.tick(), run_id="run-1")
        loop.baseline(self.home, run_id, self.tick())
        return run_id

    def attempt(self, run_id, statement, family="cache-size", **changes):
        """Register a hypothesis, apply the change, finish. Returns the decision."""
        hypothesis = {
            "statement": statement,
            "rationale": "synthetic rationale",
            "falsification": "throughput does not rise by 5% or tests fail",
            "family": family,
        }
        hypothesis_id = loop.hypothesis_add(self.home, run_id, hypothesis, self.tick())
        loop.experiment_start(self.home, run_id, hypothesis_id, self.tick())
        self.settings(**changes)
        return loop.experiment_finish(
            self.home, run_id, hypothesis_id, self.tick(), cost=1.0, queue=self.queue
        )

    def next(self, run_id):
        return loop.next_action(loop.load(self.home, run_id))


class EvolutionCase(HarnessCase):
    """Adds the synthetic harness repository, its evaluation suite and local adapter scripts."""

    def setUp(self):
        super().setUp()
        self.evo = self.tmp / "evolution"
        shutil.copytree(EXAMPLES / "evolution", self.evo)
        self.subject = self.evo / "subject"
        init_repo(self.subject)
        self.suite_path = self.evo / "suite.json"
        self.edit_suite(lambda suite: suite["runner"]["argv"].__setitem__(0, sys.executable))
        self.config_file = json.loads((self.evo / "config.json").read_text(encoding="utf-8"))
        for adapter in self.config_file["agents"].values():
            adapter["argv"] = [sys.executable, str(self.evo / adapter["argv"][1])]
        # Hook records come from real work and are classified internal; these tests allow that.
        self.write_config(
            llm_policy={"allowed_classifications": ["public", "synthetic", "internal"]}
        )

    def edit_suite(self, change):
        suite = json.loads(self.suite_path.read_text(encoding="utf-8"))
        change(suite)
        self.suite_path.write_text(
            json.dumps(suite, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def write_config(self, **sections):
        self.config_file.update(sections)
        store.write_json(self.home / "config.json", self.config_file)
        self.config = store.load_config(self.home)
        return self.config

    def approve_suite(self):
        return evolve.suite_approve(
            self.home, self.suite_path, "Hanako", "合成ケースを確認した", self.tick()
        )

    def tool_failures(self, count=3, projects=("/work/alpha", "/work/beta"), tool="Bash"):
        for index in range(count):
            payload = {
                "hook_event_name": "PostToolUseFailure",
                "session_id": f"session-{index}",
                "cwd": projects[index % len(projects)],
                "tool_name": tool,
                "tool_use_id": f"toolu-{tool}-{index}",
                "error": "exit status 1",
                "tool_input": {"command": "never stored"},
            }
            record.record(payload, self.home, self.config, self.queue, self.tick(), {})

    def manual_candidate(self, text, pattern="tool_failure:Bash"):
        """Register a hypothesis by hand and freeze a candidate whose skill.md is `text`."""
        evolve.analyze(self.home, self.config)
        hypothesis = {
            "pattern": pattern,
            "cause": "skill",
            "statement": "synthetic statement",
            "falsification": "the suite pass rate does not rise",
        }
        hypothesis_id = evolve.hypothesis_add(self.home, hypothesis, self.tick())
        worktree = evolve.candidate_create(self.home, hypothesis_id)
        (worktree / "skill.md").write_text(text, encoding="utf-8")
        evolve.candidate_freeze(self.home, hypothesis_id, self.tick())
        return hypothesis_id
