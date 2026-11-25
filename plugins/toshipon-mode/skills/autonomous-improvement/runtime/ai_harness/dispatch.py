"""Scheduler tick and queue worker. Started by launchd or by hand, never by a hook."""

import os
import shutil
import tempfile
import time
from datetime import timedelta
from pathlib import Path

from . import agents, evolve, loop, status
from .jobs import Queue
from .redact import redact_text
from .store import (
    ACTIVE_ENV,
    HarnessError,
    ensure_home,
    load_config,
    locked,
    utcnow,
    write_private,
)

AGENT_KINDS = ("audit", "evolve")


def slot_for(entry, local):
    """The most recent scheduled moment at or before `local` (a timezone-aware local time)."""
    slot = local.replace(hour=entry["hour"], minute=entry["minute"], second=0, microsecond=0)
    if "weekday" in entry:
        slot -= timedelta(days=(slot.weekday() - entry["weekday"]) % 7)
    if slot > local:
        slot -= timedelta(days=7 if "weekday" in entry else 1)
    return slot


def schedule_tick(home, config, queue, now, local=None):
    """Queue each schedule entry's latest slot once. A slot missed while asleep is picked up here."""
    local = now.astimezone() if local is None else local
    queued = []
    for entry in config["schedule"]:
        slot = slot_for(entry, local)
        job_id = f"{entry['kind']}:{slot:%Y-%m-%dT%H:%M}"
        if queue.enqueue(job_id, entry["kind"], {"slot": slot.isoformat()}, now):
            queued.append(job_id)
    return queued + loop.enqueue_due_audits(home, queue, now)


def audit_payload(state, triggers):
    """What the auditor sees: the frozen goal, every hypothesis and every decision, as data."""
    spec = state["spec"]
    return {
        "objective": spec["objective"],
        "constraints": spec["constraints"],
        "acceptance": spec["acceptance"],
        "criteria_hash": state["criteria_hash"],
        "triggers": triggers,
        "baseline": state["baseline"]["evaluation"]["metrics"],
        "best": state["best"]["evaluation"]["metrics"],
        "hypotheses": [
            {
                key: h.get(key)
                for key in ("hypothesis_id", "statement", "rationale", "falsification", "status")
            }
            for h in state["hypotheses"].values()
        ],
        "decisions": [
            {
                "ledger_seq": d["seq"],
                "hypothesis_id": d["hypothesis_id"],
                "verdict": d["verdict"],
                "reasons": d["reasons"],
                "measurements": d["measurements"],
                "evaluation_id": d["evaluation"]["id"],
                "files": d["diff"]["files"],
            }
            for d in state["decisions"]
        ],
    }


def _handle_audit(context, job):
    home, config, now = context["home"], context["config"], context["now"]
    run_id = job["payload"]["run_id"]
    state = loop.load(home, run_id)
    step = loop.next_action(state)
    if step["action"] != "audit":
        return {"action": "none", "reason": "an audit is no longer due"}
    classification = state["spec"].get("data_classification", "internal")
    audit, _ = agents.invoke(
        config,
        "auditor",
        audit_payload(state, step["triggers"]),
        home,
        classification,
        context["dry_run"],
        context["budget"],
    )
    audit_id = loop.audit_record(home, run_id, audit, "separate_process", now)
    return {
        "action": "audited",
        "run_id": run_id,
        "audit_id": audit_id,
        "findings": len(audit["findings"]),
    }


def _handle_evolve(context, job):
    home, now = context["home"], context["now"]
    return evolve.daily(
        home,
        context["config"],
        now,
        context["dry_run"],
        should_cancel=lambda: context["queue"].cancel_requested(job["id"]),
        budget=context["budget"],
    )


def _handle_weekly_report(context, job):
    path = Path(context["home"]) / "reports" / f"weekly-{context['now']:%Y-%m-%d}.md"
    write_private(path, status.weekly_report(context["home"], context["config"], context["now"]))
    return {"action": "reported", "report": str(path)}


HANDLERS = {
    "audit": _handle_audit,
    "evolve": _handle_evolve,
    "weekly_report": _handle_weekly_report,
}


def dispatch(home, config, dry_run=False, max_jobs=None, clock=utcnow, handlers=None):
    """Work the queue under an exclusive lock, within the job, time and cost budgets."""
    if os.environ.get(ACTIVE_ENV):
        raise HarnessError("dispatch does not run inside a process the harness started")
    handlers = HANDLERS if handlers is None else handlers
    budget = config["budget"]
    limit = min(max_jobs or budget["max_jobs_per_dispatch"], budget["max_jobs_per_dispatch"])
    queue = Queue(home, config["queue"])
    results, started, skip = [], time.monotonic(), ()
    with locked(Path(home) / "dispatch.lock", blocking=False):
        while (
            len(results) < limit and time.monotonic() - started < budget["max_seconds_per_dispatch"]
        ):
            now = clock()
            job = queue.claim(now, skip_kinds=skip)
            if not job:
                break
            entry = {"id": job["id"], "kind": job["kind"], "attempt": job["attempts"]}
            spend = agents.Budget(home, now, budget["max_cost_per_day"])
            context = {
                "home": home,
                "config": config,
                "now": now,
                "dry_run": dry_run,
                "queue": queue,
                "budget": spend,
            }
            try:
                if job["kind"] in AGENT_KINDS and spend.exhausted():
                    raise agents.BudgetExceeded("daily cost budget exhausted")
                handler = handlers.get(job["kind"])
                if not handler:
                    raise HarnessError(f"no handler for job kind {job['kind']}")
                outcome = handler(context, job)
            except agents.BudgetExceeded as waiting:
                # Not a failure: the job keeps its attempts and waits for tomorrow's budget.
                # Jobs that need no agent still run.
                queue.release(job["id"], clock(), str(waiting))
                results.append({**entry, "status": "deferred", "reason": str(waiting)})
                skip = AGENT_KINDS
                continue
            except agents.WouldRun as planned:
                outcome = {"action": "would_run", "reason": str(planned)}
                entry["status"] = queue.complete(job["id"], clock(), outcome)
            except Exception as error:  # a failed job must not take the worker down with it
                outcome = {"error": redact_text(f"{type(error).__name__}: {error}")[:500]}
                entry["status"] = queue.fail(job["id"], clock(), outcome["error"])
            else:
                entry["status"] = queue.complete(job["id"], clock(), outcome)
            results.append({**entry, "result": outcome})
    return results


def _not_copied(directory, names):
    """Left out of the rehearsal copy: worktrees, locks, and links (a link would write through)."""
    return [
        name
        for name in names
        if name == "worktrees" or name.endswith(".lock") or (Path(directory) / name).is_symlink()
    ]


def dry_run(home, now, tick=True):
    """Rehearse a scheduled start on a throwaway copy of the state home. The real state is not written.

    Adapters and runners execute only when marked dry_run_safe; anything else is reported as would_run.
    """
    shadow_root = Path(tempfile.mkdtemp(prefix="ai-harness-dry-run-"))
    try:
        shadow = shadow_root / "state"
        shutil.copytree(home, shadow, ignore=_not_copied)
        shadow = ensure_home(shadow)
        config = load_config(shadow)
        queue = Queue(shadow, config["queue"])
        queued = schedule_tick(shadow, config, queue, now) if tick else []
        results = dispatch(shadow, config, dry_run=True, clock=lambda: now)
        reports = {
            str(path.relative_to(shadow)): path.read_text(encoding="utf-8")
            for path in sorted(shadow.glob("evolution/proposals/*/report.md"))
            if not (Path(home) / path.relative_to(shadow)).exists()
        }
        return {"dry_run": True, "queued": queued, "jobs": results, "new_proposal_reports": reports}
    finally:
        evolve.cleanup(shadow_root / "state")
        shutil.rmtree(shadow_root, ignore_errors=True)
