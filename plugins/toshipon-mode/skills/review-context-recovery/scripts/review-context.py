#!/usr/bin/env python3
"""Launcher: resolves the runtime, and the Project Context Explorer skill next to it, from its own location."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))

try:
    from review_recovery.cli import main
except ImportError as missing:  # the skills this one shares its evidence checks with are not there
    print(
        json.dumps(
            {
                "error": f"project-context-explorer と autonomous-improvement skill が必要です: {missing}"
            }
        ),
        file=sys.stderr,
    )
    sys.exit(2)

if __name__ == "__main__":
    sys.exit(main())
