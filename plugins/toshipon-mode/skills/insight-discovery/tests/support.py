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

from insight_discovery import evidence, foundation, signals  # noqa: E402, I001
from ai_harness import record as hook_record  # noqa: E402  (importable once foundation is loaded)
from ai_harness import store  # noqa: E402
from insight_discovery import state as st  # noqa: E402
from insight_discovery.foundation import GateFailed, HarnessError, Refusal  # noqa: E402

__all__ = [
    "EXAMPLES",
    "GateFailed",
    "HarnessError",
    "InsightCase",
    "ROOT",
    "Refusal",
    "evidence",
    "foundation",
    "hook_record",
    "signals",
    "st",
    "store",
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
