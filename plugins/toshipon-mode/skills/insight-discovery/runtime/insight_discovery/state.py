"""Where Insight state lives, its settings, and the ledger folded into the current picture.

Nothing is kept between commands: every command replays the append-only ledger, so an
interrupted run continues from the last record it wrote.
"""

import copy
from pathlib import Path

from .foundation import SCHEMAS, append_log, read_json, read_log, validate

LOG_ID = "insight"
STAGES = ("observe", "structure", "hypothesize", "audit", "assess")
INDEPENDENT = ("separate_process", "subagent")
MAX_PRESENTED = 3
DEFAULT_CONFIG = {
    "schema_version": 1,
    "sources": [{"adapter": "ledger"}, {"adapter": "claude_insights"}],
    "limits": {
        "max_evidence_per_cycle": 60,
        "max_llm_calls_per_run": 6,
        "max_seconds_per_run": 900,
        "max_file_bytes": 1_000_000,
        "max_files_per_source": 500,
        "max_text_chars": 2000,
    },
    "gates": {
        "min_evidence": 2,
        "min_distinct_contexts": 2,
        "similarity": 0.85,
        "max_candidates": MAX_PRESENTED,
    },
    "agents": {},
    "search": {"allowed_domains": [], "max_queries": 5, "max_query_chars": 120},
}


def root(home):
    return Path(home) / "insight"


def ledger_path(home):
    return root(home) / "ledger.jsonl"


def load_config(home):
    """User settings merged over the defaults, one level deep."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    user = validate(f"{SCHEMAS}:insight-config", read_json(root(home) / "config.json", {}))
    for key, value in user.items():
        if isinstance(value, dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def append(home, record_type, data, now):
    return append_log(ledger_path(home), LOG_ID, record_type, data, now, schema=f"{SCHEMAS}:ledger")


def week_of(moment):
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def _transition(candidate, status, record, actor, reason):
    candidate["history"].append(
        {
            "from": candidate["status"],
            "to": status,
            "at": record["created_at"],
            "actor": actor,
            "reason": reason,
        }
    )
    candidate["status"] = status


def fold(records):
    state = {
        "evidence": {},
        "evidence_seq": {},
        "ingests": {},
        "cycles": {},
        "open_cycle": None,
        "candidates": {},
        "resolved_seq": {},
        "handoffs": [],
        "priors": {},
        "prior_skips": {},
        "selections": {},
        "reports": [],
        "searches": {},
        "budget_stops": [],
        "last_seq": 0,
    }
    for record in records:
        kind, data = record["type"], record["data"]
        state["last_seq"] = record["seq"]
        candidate = state["candidates"].get(data.get("insight_id"))
        if kind == "ingested":
            state["ingests"][data["adapter"]] = {**data["stats"], "at": record["created_at"]}
            for item in data["evidence"]:
                state["evidence"][item["id"]] = item
                state["evidence_seq"][item["id"]] = record["seq"]
        elif kind == "cycle_started":
            state["cycles"][data["cycle_id"]] = {**data, "stages": {}, "finished": None}
            state["open_cycle"] = data["cycle_id"]
        elif kind == "stage_recorded":
            state["cycles"][data["cycle_id"]]["stages"][data["stage"]] = data
        elif kind == "budget_exhausted":
            state["budget_stops"].append({**data, "at": record["created_at"]})
        elif kind == "cycle_finished":
            state["cycles"][data["cycle_id"]]["finished"] = {**data, "at": record["created_at"]}
            state["open_cycle"] = None
        elif kind == "candidate_registered":
            state["candidates"][data["id"]] = copy.deepcopy(data)
        elif kind == "handoff_recorded":
            state["handoffs"].append({**data, "at": record["created_at"]})
            if candidate:
                candidate["routing"] = {
                    "disposition": f"handoff_{data['target']}",
                    "reason": data["reason"],
                    "handoff_ref": data["handoff_ref"],
                }
        elif kind == "prior_recorded":
            state["priors"][data["week"]] = {
                "text": data["text"],
                "recorded_at": record["created_at"],
            }
        elif kind == "prior_skipped":
            state["prior_skips"][data["week"]] = data["reason"]
        elif kind == "weekly_reported":
            state["reports"].append({**data, "at": record["created_at"]})
        elif kind == "selected" and candidate:
            approval = data["approval"]
            _transition(candidate, "accepted", record, approval["reviewer"], approval["reason"])
            candidate["human_prior"] = state["priors"].get(data["week"])
            state["selections"][data["week"]] = data["insight_id"]
        elif kind == "resolved" and candidate:
            _transition(candidate, data["status"], record, data["actor"], data["reason"])
            state["resolved_seq"][data["insight_id"]] = record["seq"]
        elif kind == "search_approved":
            state["searches"].setdefault(data["insight_id"], []).append(data)
        elif kind == "prior_art_recorded" and candidate:
            candidate["prior_art"] = data["prior_art"]
            candidate["novelty"] = data["novelty"]
    return state


def load(home):
    return fold(read_log(ledger_path(home)))


def analyzed(state):
    """Evidence ids some cycle has already looked at."""
    return {item for cycle in state["cycles"].values() for item in cycle["evidence_ids"]}


def fresh(state):
    seen = analyzed(state)
    return [item for item in state["evidence"] if item not in seen]
