#!/usr/bin/env python3
"""Launcher: resolves the runtime from its own location, wherever the skill is installed."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

from ai_harness.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
