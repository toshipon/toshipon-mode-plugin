"""Shared fixtures: a throwaway state home and the synthetic records."""

import json
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runtime"))

sys.path.insert(0, str(ROOT / "examples" / "synthetic"))
sys.path.insert(0, str(ROOT.parent / "autonomous-improvement" / "runtime"))

import stub_agent  # noqa: E402

from ai_harness import record as hook_record  # noqa: E402
from ai_harness import store  # noqa: E402
from insight_discovery import (  # noqa: E402
    checks,
    cli,
    decisions,
    evidence,
    foundation,
    pipeline,
    ranking,
    report,
    routing,
    signals,
)
from insight_discovery import state as st  # noqa: E402
from insight_discovery.foundation import GateFailed, HarnessError, Refusal  # noqa: E402

__all__ = [
    "Backend",
    "EXAMPLES",
    "GateFailed",
    "HarnessError",
    "InsightCase",
    "ROOT",
    "Refusal",
    "evidence",
    "decisions",
    "checks",
    "cli",
    "foundation",
    "hook_record",
    "pipeline",
    "ranking",
    "report",
    "routing",
    "signals",
    "st",
    "store",
    "stub_agent",
]

EXAMPLES = ROOT / "examples" / "synthetic"
START = datetime(2026, 1, 5, 9, 0, tzinfo=timezone.utc)  # a Monday


class InsightCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="insight-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.home = store.ensure_home(self.tmp / "state")
        self.clock = START
        self.config = foundation.load_config(self.home)
        self.settings = st.load_config(self.home)

    def tick(self, seconds=60):
        self.clock += timedelta(seconds=seconds)
        return self.clock

    def write(self, name, content):
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        path.write_text(text, encoding="utf-8")
        return path

    def configure(self, **sections):
        """Write Insight settings and reload them."""
        st.root(self.home).mkdir(mode=0o700, exist_ok=True)
        foundation.write_json(st.root(self.home) / "config.json", sections)
        self.settings = st.load_config(self.home)
        return self.settings

    def configure_foundation(self, **sections):
        foundation.write_json(self.home / "config.json", sections)
        self.config = foundation.load_config(self.home)
        return self.config

    def ingest(self, adapter, path=None):
        return evidence.ingest(self.home, adapter, path, self.config, self.settings, self.tick())

    def ingest_synthetic(self):
        return self.ingest("manual", EXAMPLES / "records")

    def state(self):
        return st.load(self.home)


class Backend:
    """Stands in for the model: the stub's answers, optionally altered by a test, with every call kept."""

    def __init__(self, **alter):
        self.calls, self.alter = [], alter

    def __call__(self, stage, body, classification):
        self.calls.append((stage, body, classification))
        output = stub_agent.answer(body)
        if stage in self.alter:
            output = self.alter[stage](output, body) or output
        return output

    def stages(self):
        return [stage for stage, _, _ in self.calls]


class LoopCase(InsightCase):
    """Synthetic records as the only source, and a scripted backend instead of a model."""

    def setUp(self):
        super().setUp()
        self.configure(sources=[{"adapter": "manual", "path": str(EXAMPLES / "records")}])

    def run_daily(self, backend=None, **options):
        backend = backend or Backend()
        summary = pipeline.run_daily(
            self.home, self.config, self.settings, self.tick(), invoke=backend, **options
        )
        return summary, backend

    def candidates(self):
        return self.state()["candidates"]
