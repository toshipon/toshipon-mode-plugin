"""The one place that reaches into the Project Context Explorer skill next to this one.
The claim contract, evidence snapshots, read-only git and the untrusted envelope live there."""

import sys
from pathlib import Path

sys.path.insert(
    0, str(Path(__file__).resolve().parents[3] / "project-context-explorer" / "runtime")
)

from context_recovery import gitread  # noqa: E402
from context_recovery.base import (  # noqa: E402
    Refusal,
    append_private,
    dump,
    now,
    project_dir,
    read_json,
    read_jsonl,
    read_text,
)
from context_recovery.claims import parse_locator, verify  # noqa: E402
from context_recovery.evidence import Evidence  # noqa: E402
from context_recovery.redaction import redact  # noqa: E402
from context_recovery.untrusted import PREAMBLE, envelope, one_line, plain_path  # noqa: E402

__all__ = [
    "Evidence",
    "PREAMBLE",
    "Refusal",
    "append_private",
    "dump",
    "envelope",
    "gitread",
    "now",
    "one_line",
    "plain_path",
    "parse_locator",
    "project_dir",
    "read_json",
    "read_jsonl",
    "read_text",
    "redact",
    "verify",
]
