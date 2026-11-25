"""Hook entry point: one small structured record, and a queue entry when an audit is due.

It starts no model and no other process, and does nothing at all inside a harness child.
"""

import json
import re
from pathlib import Path

from . import loop
from .redact import redact_text
from .store import ACTIVE_ENV, iso, locked, sha256, validate

KINDS = {
    "Stop": "session_stop",
    "SubagentStop": "subagent_stop",
    "SessionEnd": "session_end",
    "PostToolUseFailure": "tool_failure",
}
DETAIL_LIMIT = 500
TOOL_NAME = re.compile(r"[A-Za-z0-9_.:-]{1,100}")


def build_event(payload, config, now):
    """Reduce a hook payload to what analysis needs. Paths and session ids are stored hashed."""
    kind = KINDS.get(payload.get("hook_event_name"))
    if not kind:
        return None
    session = str(payload.get("session_id", ""))
    unique = payload.get("tool_use_id") or iso(now)
    event = {
        "schema_version": 1,
        "id": sha256(f"{session}|{kind}|{unique}"),
        "created_at": iso(now),
        "kind": kind,
        "project": sha256(str(payload.get("cwd", "")))[:16],
        "session": sha256(session)[:16],
    }
    if kind == "tool_failure":
        # The name ends up in pattern signatures, adapter payloads and reports, and an MCP
        # server chooses its own tool names: keep identifiers, drop anything else.
        name = str(payload.get("tool_name", ""))
        event["tool_name"] = name if TOOL_NAME.fullmatch(name) else "unknown"
        event["is_interrupt"] = bool(payload.get("is_interrupt"))
        if config["collect"]["raw"] and payload.get("error"):
            event["detail"] = redact_text(str(payload["error"]))[:DETAIL_LIMIT]
    return validate("event", event)


def record(payload, home, config, queue, now, env):
    """Append the event once (by id) and queue due audits for this project. Returns the event or None."""
    if env.get(ACTIVE_ENV):
        return None  # fired by a process this harness started; recording it would recurse
    event = build_event(payload, config, now)
    if not event:
        return None
    path = Path(home) / "events.jsonl"
    with locked(Path(home) / "events.lock"):
        seen = path.read_text(encoding="utf-8") if path.exists() else ""
        if f'"id": "{event["id"]}"' in seen:
            return None
        with open(path, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        path.chmod(0o600)
    if event["kind"] == "session_stop" and payload.get("cwd"):
        loop.enqueue_due_audits(home, queue, now, project=Path(payload["cwd"]).resolve())
    return event
