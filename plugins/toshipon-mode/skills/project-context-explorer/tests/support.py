"""Shared fixtures: a throwaway state home and the synthetic project as a git repository."""

import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "synthetic"
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(EXAMPLES))

import synthetic_repo  # noqa: E402

from context_recovery import cli  # noqa: E402

__all__ = ["EXAMPLES", "ROOT", "ExplorerCase", "load", "synthetic_repo"]


def load(name):
    return json.loads((EXAMPLES / name).read_text(encoding="utf-8"))


class ExplorerCase(unittest.TestCase):
    """A repository at the base commit, with the synthetic entities indexed there.
    `self.change()` then moves it to the head commit, which is what makes references stale."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="context-explorer-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "tidepool"
        self.repo.mkdir()
        self.base = synthetic_repo.build_base(self.repo)
        self.head = self.base
        previous = {name: os.environ.get(name) for name in ("AI_HARNESS_HOME", "AI_HARNESS_NOW")}
        os.environ["AI_HARNESS_HOME"] = str(self.tmp / "state")
        os.environ["AI_HARNESS_NOW"] = "2026-03-10T09:00:00+00:00"
        self.addCleanup(self._restore, previous)

    @staticmethod
    def _restore(previous):
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def change(self):
        self.head = synthetic_repo.apply_change(self.repo)
        return self.head

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(path)

    def run_cli(self, *args, expect=0):
        """(parsed stdout, parsed stderr) of one command."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--root", str(self.repo), *args])
        self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        return (
            json.loads(out.getvalue()) if out.getvalue() else None,
            json.loads(err.getvalue()) if err.getvalue() else None,
        )

    def ok(self, *args):
        return self.run_cli(*args)[0]

    def refused(self, *args):
        return self.run_cli(*args, expect=2)[1]

    def index_synthetic(self):
        self.ok("index", "add", "--file", str(EXAMPLES / "entities.json"))
        self.ok("sources", "--file", str(EXAMPLES / "sources.json"))

    def claims(self):
        """The synthetic claims with their revisions filled in, and the label of each."""
        text = (EXAMPLES / "claims.json").read_text(encoding="utf-8")
        claims = json.loads(text.replace("{{BASE}}", self.base).replace("{{HEAD}}", self.head))[
            "claims"
        ]
        expected = {claim["id"]: claim.pop("expected") for claim in claims}
        return claims, expected
