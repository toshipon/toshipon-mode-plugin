"""Secret shapes come from the Autonomous Improvement skill next to this one, so the two
skills cannot disagree on what a secret looks like."""

import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "autonomous-improvement" / "runtime"))

from ai_harness import redact as _shapes  # noqa: E402


def redact_text(text: str) -> str:
    return _shapes.redact_text(text)


def redact(value: Any) -> Any:
    """A copy of a JSON value with every secret-shaped substring replaced."""
    return _shapes.redact(value)
