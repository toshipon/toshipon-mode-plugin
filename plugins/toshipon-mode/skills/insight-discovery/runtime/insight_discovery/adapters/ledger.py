"""The foundation's own records: hook events and the ledgers of improvement runs.

Failures, rejected experiments, human unblocks and audit findings are read as counts and
enum values. Hypothesis text, diffs and commands stay where they are.
"""

import json
from collections import Counter
from pathlib import Path

from ..foundation import HarnessError, loop, read_log, sha256, validate


def _events(home, stats):
    path = Path(home) / "events.jsonl"
    if not path.is_file() or path.is_symlink():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = validate("event", json.loads(line))
        except (ValueError, HarnessError):
            stats["format_unrecognized"] += 1
            continue
        if event["kind"] != "tool_failure":
            continue
        interrupted = event.get("is_interrupt")
        yield {
            "kind": "manual_intervention" if interrupted else "failure",
            "label": f"tool_{'interrupt' if interrupted else 'failure'}:{event['tool_name']}",
            "task_key": f"project:{event['project']}",
            "domain_tag": "hook-events",
            "unit": event["id"][:16],
            "locator": f"events/{event['id'][:12]}",
        }


def _run_records(home, stats):
    for run_id in loop.list_runs(home):
        try:
            records = read_log(loop.ledger_path(home, run_id))
        except HarnessError:
            stats["corrupt_ledger"] += 1
            continue
        task = f"run:{sha256(run_id)[:12]}"
        for record in records:
            data, found = record["data"], []
            if record["type"] == "decision_recorded" and data["verdict"] != "adopt":
                kind = "hypothesis_outcome" if data["verdict"] == "reject" else "failure"
                if data["verdict"] == "needs_human":
                    kind = "manual_intervention"
                found.append((kind, f"decision:{data['verdict']}"))
            elif record["type"] == "unblocked":
                found.append(("manual_intervention", "run:unblocked"))
            elif record["type"] == "audit_recorded":
                found += [
                    ("review_finding", f"audit:{finding['category']}")
                    for finding in data.get("findings", [])
                ]
            for index, (kind, label) in enumerate(found):
                yield {
                    "kind": kind,
                    "label": label,
                    "task_key": task,
                    "domain_tag": "improvement-runs",
                    "unit": sha256(f"{record['id']}|{index}")[:16],
                    "locator": f"runs/{task[4:]}/{record['seq']}",
                }


def collect(path, context):
    stats = Counter()
    home = context["home"]
    raws = [
        {**raw, "classification": "internal"}
        for source in (_events(home, stats), _run_records(home, stats))
        for raw in source
    ]
    return raws, stats
