"""The one place that imports the Autonomous Improvement runtime (ai_harness).

Queue, locks, the hash-chained ledger, redaction, human gates, the scheduler tick and the agent
adapters all live there. Insight Discovery only registers its schemas and its model roles.
"""

import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[2]
FOUNDATION_RUNTIME = SKILL_ROOT.parent / "autonomous-improvement" / "runtime"

if not (FOUNDATION_RUNTIME / "ai_harness").is_dir():
    raise ImportError(
        "Insight Discovery needs the autonomous-improvement skill next to it: "
        f"{FOUNDATION_RUNTIME} was not found"
    )
if str(FOUNDATION_RUNTIME) not in sys.path:
    sys.path.insert(0, str(FOUNDATION_RUNTIME))

from ai_harness import agents, dispatch, loop  # noqa: E402
from ai_harness.jobs import Queue  # noqa: E402
from ai_harness.redact import (  # noqa: E402
    find_deny_terms,
    find_secrets,
    load_deny_terms,
    redact_text,
)
from ai_harness.store import (  # noqa: E402
    ACTIVE_ENV,
    AUTOMATED_ENV,
    Busy,
    HarnessError,
    append_log,
    canonical,
    iso,
    load_config,
    locked,
    parse_time,
    read_json,
    read_log,
    register_schemas,
    require_human,
    sha256,
    state_home,
    utcnow,
    validate,
    write_json,
    write_private,
)

__all__ = [
    "ACTIVE_ENV",
    "AUTOMATED_ENV",
    "Busy",
    "GateFailed",
    "HarnessError",
    "Queue",
    "Refusal",
    "SCHEMAS",
    "STAGE_ROLES",
    "agents",
    "append_log",
    "canonical",
    "dispatch",
    "find_deny_terms",
    "find_secrets",
    "iso",
    "load_config",
    "load_deny_terms",
    "locked",
    "loop",
    "parse_time",
    "read_json",
    "read_log",
    "redact_text",
    "require_human",
    "sha256",
    "state_home",
    "utcnow",
    "validate",
    "write_json",
    "write_private",
]

SCHEMAS = "insight-discovery"
register_schemas(SCHEMAS, SKILL_ROOT / "schemas")

# stage -> (role name, output schema, which adapter of insight config `agents` runs it).
# No role gets a tool: every stage reads the payload and returns JSON.
STAGE_ROLES = {
    "observe": ("insight_observe", f"{SCHEMAS}:stages#observe_output", "analyst"),
    "structure": ("insight_structure", f"{SCHEMAS}:stages#structure_output", "analyst"),
    "hypothesize": ("insight_hypothesize", f"{SCHEMAS}:stages#hypothesize_output", "analyst"),
    "audit": ("insight_audit", f"{SCHEMAS}:insight-audit#output", "auditor"),
    "assess": ("insight_assess", f"{SCHEMAS}:stages#assess_output", "analyst"),
}
for _role, _schema, _adapter in STAGE_ROLES.values():
    agents.ROLES[_role] = {
        "schema": _schema,
        "tools": "",
        "agent": "insight-auditor" if _adapter == "auditor" else "insight-discovery",
    }


class Refusal(HarnessError):
    """A safety boundary said no (path, secret, policy, automation). The CLI exits 3."""


class GateFailed(HarnessError):
    """A quality gate did not pass. The CLI exits 1."""
