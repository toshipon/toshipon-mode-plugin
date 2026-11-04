#!/usr/bin/env python3
"""Run the bundled harness without relying on the current working directory."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from harness.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
