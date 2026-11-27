"""Shared fixtures: the synthetic project as a repository, and a session over its synthetic PR #42."""

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
PROJECT = ROOT.parent / "project-context-explorer" / "examples" / "synthetic"
sys.path.insert(0, str(ROOT / "runtime"))
sys.path.insert(0, str(PROJECT))

import synthetic_repo  # noqa: E402

from review_recovery import cli  # noqa: E402

__all__ = ["EXAMPLES", "ReviewCase"]


class ReviewCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="review-context-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.repo = self.tmp / "tidepool"
        self.repo.mkdir()
        built = synthetic_repo.build(self.repo)
        self.base, self.head = built["base"], built["head"]
        names = ("AI_HARNESS_HOME", "AI_HARNESS_NOW", "AI_HARNESS_AUTOMATED", "AI_HARNESS_ACTIVE")
        previous = {name: os.environ.get(name) for name in names}
        self.addCleanup(self._restore, previous)
        for name in names:
            os.environ.pop(name, None)
        os.environ["AI_HARNESS_HOME"] = str(self.tmp / "state")
        self.at("09:00:00")

    @staticmethod
    def _restore(previous):
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def at(self, clock):
        os.environ["AI_HARNESS_NOW"] = f"2026-03-10T{clock}+00:00"

    def write(self, name, value):
        path = self.tmp / name
        path.write_text(
            value if isinstance(value, str) else json.dumps(value, ensure_ascii=False),
            encoding="utf-8",
        )
        return str(path)

    def run_cli(self, *args, expect=0, pr=42):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--root", str(self.repo), "--pr", str(pr), *args])
        self.assertEqual(code, expect, err.getvalue() or out.getvalue())
        return json.loads(out.getvalue()) if code == 0 else json.loads(err.getvalue())

    def ok(self, *args, **options):
        return self.run_cli(*args, **options)

    def refused(self, *args, **options):
        return self.run_cli(*args, expect=2, **options)

    def start(self, *extra):
        threads = str(EXAMPLES / "threads.json")
        return self.ok(
            "start", "--base", self.base, "--head", self.head, "--threads", threads, *extra
        )

    def explanation(self, name="9001.json", **changes):
        text = (EXAMPLES / "explanations" / name).read_text(encoding="utf-8")
        explanation = json.loads(text.replace("{{BASE}}", self.base).replace("{{HEAD}}", self.head))
        return {**explanation, **changes}

    def explain(self, explanation=None, expect=0):
        path = self.write("explanation.json", explanation or self.explanation())
        return self.run_cli("explain", "--file", path, expect=expect)

    def walk_through(self):
        """Explain, answer and recap the current comment."""
        self.explain()
        self.ok("answer", "--text", "2 回", "--assessment", "correct")
        self.ok(
            "recap",
            "--intent",
            "再試行を早める",
            "--risk",
            "二重課金",
            "--next-action",
            "45 秒に戻すか相談する",
        )
