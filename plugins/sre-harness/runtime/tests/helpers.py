"""Helpers shared by the review tests. Fixtures that use them live in conftest.py."""

import io
import json
from pathlib import Path

from examples.synthetic.github_responses import NOW
from sre_harness import cli

TOKEN = "synthetic-token-value-0123456789"
TOKEN_ENV = "REVIEW_READ_TOKEN"
PERIOD = ["--since", "2026-01-01T00:00:00Z", "--until", "2026-01-31T00:00:00Z"]


class FakeClock:
    """Epoch seconds that move only when the harness sleeps."""

    def __init__(self, now=NOW):
        self.now = now
        self.slept = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def run_cli(argv, env):
    """(exit code, parsed stdout or None)."""
    out = io.StringIO()
    code = cli.main(argv, env=env, out=out)
    text = out.getvalue()
    return code, (json.loads(text) if text.strip() else None)


def collect_fixture(directory, env, *extra, period=PERIOD):
    argv = ["review", "collect", "--repo", "example-org/demo", *period, "--fixture"]
    return run_cli([*argv, str(directory), *extra], env)


def run_dir(env, run_id):
    return Path(env["AI_HARNESS_HOME"]) / "sre-harness" / "runs" / run_id


def read_jsonl(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
