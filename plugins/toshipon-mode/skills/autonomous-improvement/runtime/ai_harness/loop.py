"""Product improvement loop: ledger fold, next-action table, plateau detection, audits.

State is never stored. Every command folds the append-only ledger, so a crashed session
resumes from whatever the ledger last recorded.
"""

import difflib
import re
from collections import Counter
from pathlib import Path, PurePosixPath

from . import evaluation, guard
from .redact import redact
from .store import (
    HarnessError,
    append_log,
    parse_time,
    read_log,
    require_human,
    validate,
)

COMPUTED_STOPS = ("passed", "budget_exhausted", "no_improvement", "unevaluable")
DECLARED_STOPS = ("permission_denied", "needs_human", "cancelled")
BLOCKING = ("critical", "high")
INDEPENDENT = ("separate_process", "subagent")
RESOLUTIONS = ("confirmed", "refuted", "unverifiable")
DEFAULT_PLATEAU = {"same_failure": 3, "no_improvement": 3, "max_audits_without_adopt": 1}
DEFAULT_SIMILARITY = 0.85
RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,80}$")


def run_dir(home, run_id):
    if not RUN_ID.match(run_id):
        raise HarnessError("run id must be lowercase letters, digits and hyphens")
    return Path(home) / "runs" / run_id


def ledger_path(home, run_id):
    return run_dir(home, run_id) / "ledger.jsonl"


def fold(events):
    state = {
        "run_id": events[0]["run_id"],
        "project": None,
        "spec": None,
        "criteria_hash": None,
        "baseline": None,
        "best": None,
        "hypotheses": {},
        "open": None,
        "decisions": [],
        "audits": [],
        "blocked": None,
        "stopped": None,
        "cost": 0.0,
        "seconds": 0.0,
        "last_seq": 0,
        "anchor": 0,  # seq of the result an audit would examine: last decision or baseline
        "last_adopt_seq": 0,
    }
    for event in events:
        kind, data, seq = event["type"], event["data"], event["seq"]
        state["last_seq"] = seq
        if kind in ("run_started", "criteria_changed"):
            state["spec"], state["criteria_hash"] = data["spec"], data["criteria_hash"]
            state["project"] = data.get("project", state["project"])
            state["baseline"] = state["best"] = None
        elif kind == "baseline_recorded":
            state["baseline"] = state["best"] = {**data, "seq": seq}
            state["anchor"] = state["last_adopt_seq"] = seq
        elif kind == "hypothesis_registered":
            state["hypotheses"][data["hypothesis_id"]] = {**data, "status": "registered"}
        elif kind == "experiment_started":
            state["open"] = {**data, "started_at": event["created_at"]}
            state["hypotheses"][data["hypothesis_id"]]["status"] = "running"
        elif kind == "decision_recorded":
            state["open"] = None
            state["decisions"].append({**data, "seq": seq})
            state["hypotheses"][data["hypothesis_id"]]["status"] = data["verdict"]
            state["cost"] += data["cost"]
            state["seconds"] += data["duration_seconds"]
            state["anchor"] = seq
            if data["verdict"] == "adopt":
                state["best"] = {
                    "evaluation": data["evaluation"],
                    "guard": data["guard"],
                    "seq": seq,
                }
                state["last_adopt_seq"] = seq
            elif data["verdict"] == "needs_human":
                state["blocked"] = {
                    "hypothesis_id": data["hypothesis_id"],
                    "reasons": data["reasons"],
                }
        elif kind == "audit_recorded":
            findings = {f["id"]: {**f, "status": "open"} for f in data["findings"]}
            state["audits"].append({**data, "findings": findings, "seq": seq})
        elif kind == "finding_resolved":
            audit = next(a for a in state["audits"] if a["audit_id"] == data["audit_id"])
            audit["findings"][data["finding_id"]].update(
                status=data["status"], resolution=data["evidence"]
            )
        elif kind == "unblocked":
            # Whatever the person decided about the leftover change, the old reference no longer
            # describes the work tree. Measuring again keeps the next experiment from taking its credit.
            state["blocked"] = None
            state["baseline"] = state["best"] = None
        elif kind == "run_stopped":
            state["stopped"] = data
    return state


def load(home, run_id):
    events = read_log(ledger_path(home, run_id))
    if not events:
        raise HarnessError(f"unknown run: {run_id}")
    return fold(events)


def _active(state):
    if state["stopped"]:
        raise HarnessError(f"run already stopped: {state['stopped']['reason']}")
    return state


def _append(home, state_or_id, kind, data, now):
    run_id = state_or_id if isinstance(state_or_id, str) else state_or_id["run_id"]
    return append_log(ledger_path(home, run_id), run_id, kind, data, now)


def _criteria_intact(state):
    try:
        return evaluation.criteria_hash(state["spec"], state["project"]) == state["criteria_hash"]
    except HarnessError:
        return False  # an evaluator file is gone


def assert_criteria(state):
    """The evaluator files must still be the ones that were frozen."""
    if not _criteria_intact(state):
        raise HarnessError(
            "evaluation criteria changed since they were frozen; restore the evaluator files or "
            "record a human-approved criteria-change"
        )


def _spec_commands(spec):
    commands = [spec["evaluation"]["command"]]
    return commands + [check["command"] for check in spec.get("guard", {}).get("checks", [])]


def _checked_spec(spec):
    validate("spec", spec)
    for argv in _spec_commands(spec):
        evaluation.assert_safe_argv(argv)
    for relative in _protected(spec):
        parts = PurePosixPath(relative).parts
        if (
            relative.startswith(("/", "./"))
            or ".." in parts
            or str(PurePosixPath(relative)) != relative
        ):
            raise HarnessError(
                f"{relative}: evaluator and protected paths must be plain relative paths"
            )
    if redact(spec) != spec:
        raise HarnessError(
            "the spec contains a secret-shaped string; pass credentials through the environment"
        )
    names = {metric["name"] for metric in spec["evaluation"]["metrics"]}
    for target in spec.get("acceptance", {}).get("targets", []):
        if target["metric"] not in names:
            raise HarnessError(f"acceptance target names an undeclared metric: {target['metric']}")
    return spec


def init(home, project, spec, now, run_id=None):
    _checked_spec(spec)
    project = Path(project).resolve()
    guard.require_git(project)
    run_id = run_id or f"{spec['name']}-{now:%Y%m%d%H%M%S}"
    if ledger_path(home, run_id).exists():
        raise HarnessError(f"run already exists: {run_id}")
    data = {
        "project": str(project),
        "spec": spec,
        "criteria_hash": evaluation.criteria_hash(spec, project),
    }
    _append(home, run_id, "run_started", data, now)
    return run_id


def baseline(home, run_id, now):
    state = _active(load(home, run_id))
    if state["baseline"]:
        raise HarnessError("baseline already recorded for the current criteria")
    assert_criteria(state)
    checks = guard.run_checks(state["spec"], state["project"])
    measured = evaluation.measure(
        state["spec"], state["project"], f"EV-{state['last_seq'] + 1:04d}", "baseline", now
    )
    if measured["status"] != "evaluated":
        raise HarnessError(f"baseline is unevaluable: {measured['unevaluable_reason']}")
    _append(home, state, "baseline_recorded", {"evaluation": measured, "guard": checks}, now)
    return {"evaluation": measured, "guard": checks}


def _plateau_config(spec):
    return {**DEFAULT_PLATEAU, **spec.get("plateau", {})}


def _last_audit_seq(state):
    return state["audits"][-1]["seq"] if state["audits"] else 0


def plateau_signals(state):
    """Stagnation since the last adoption or audit. Unevaluable runs say nothing either way."""
    config = _plateau_config(state["spec"])
    since = max(_last_audit_seq(state), state["last_adopt_seq"])
    recent = [
        d
        for d in state["decisions"]
        if d["seq"] > since and d["verdict"] in ("reject", "needs_human")
    ]
    signals = []
    repeated = [
        name
        for name, count in Counter(d["signature"] for d in recent).items()
        if count >= config["same_failure"]
    ]
    if repeated:
        signals.append(f"same_failure:{sorted(repeated)[0]}")
    if len(recent) >= config["no_improvement"]:
        signals.append("no_improvement")
    return signals


def _blocking_findings(audit, statuses):
    return [
        finding["id"]
        for finding in audit["findings"].values()
        if finding["severity"] in BLOCKING and finding["status"] in statuses
    ]


def _budget_exhausted(state):
    budget = state["spec"]["budget"]
    if len(state["decisions"]) >= budget["max_iterations"]:
        return "max_iterations"
    if "max_seconds" in budget and state["seconds"] >= budget["max_seconds"]:
        return "max_seconds"
    if "max_cost" in budget and state["cost"] >= budget["max_cost"]:
        return "max_cost"
    return None


def _stop(reason, detail=""):
    return {"action": "stop", "reason": reason, "detail": detail}


def next_action(state):
    """The decision table. Pure: the same ledger always yields the same next step."""
    spec = state["spec"]
    if state["stopped"]:
        return {"action": "stopped", "reason": state["stopped"]["reason"]}
    if not state["baseline"]:
        return {"action": "baseline"}
    if state["blocked"]:
        return _stop("needs_human", ", ".join(state["blocked"]["reasons"]))
    if state["open"]:
        return {"action": "finish", "hypothesis_id": state["open"]["hypothesis_id"]}
    for audit in state["audits"]:
        unresolved = _blocking_findings(audit, ("open",))
        if unresolved:
            return {
                "action": "resolve_findings",
                "audit_id": audit["audit_id"],
                "findings": unresolved,
            }
    current = [a for a in state["audits"] if a["seq"] > state["last_adopt_seq"]]
    standing = [
        f"{a['audit_id']}/{finding}"
        for a in current
        for finding in _blocking_findings(a, ("confirmed", "unverifiable"))
    ]
    if evaluation.accepted(spec, state["best"]) and not standing:
        gate = spec.get("audit", {}).get("on_completion", True)
        if not gate or any(a["independence"] in INDEPENDENT for a in current):
            return _stop("passed")
        return {"action": "audit", "triggers": ["completion"]}
    tail = 0
    for decision in reversed(state["decisions"]):
        if decision["verdict"] != "unevaluable" or decision["seq"] < state["baseline"]["seq"]:
            break
        tail += 1
    if tail >= spec["budget"].get("max_consecutive_unevaluable", 2):
        return _stop("unevaluable", f"{tail} consecutive unevaluable experiments")
    exhausted = _budget_exhausted(state)
    if exhausted:
        return _stop("budget_exhausted", exhausted)
    signals = plateau_signals(state)
    if signals:
        if len(current) >= _plateau_config(spec)["max_audits_without_adopt"]:
            return _stop("no_improvement", "stagnated again after an audit: " + ", ".join(signals))
        return {"action": "audit", "triggers": signals}
    ready = [
        h["hypothesis_id"] for h in state["hypotheses"].values() if h["status"] == "registered"
    ]
    if ready:
        return {"action": "execute", "hypothesis_id": ready[0]}
    return {"action": "hypothesize", "standing_findings": standing}


def hypothesis_add(home, run_id, hypothesis, now):
    """Register a hypothesis. A near copy of a rejected one needs new evidence."""
    validate("spec#hypothesis", hypothesis)
    state = _active(load(home, run_id))
    limit = state["spec"].get("duplicates", {}).get("similarity", DEFAULT_SIMILARITY)
    for earlier in state["hypotheses"].values():
        if (
            earlier["status"] not in ("reject", "unevaluable")
            or earlier["family"] != hypothesis["family"]
        ):
            continue
        ratio = difflib.SequenceMatcher(
            None, earlier["statement"].lower(), hypothesis["statement"].lower()
        ).ratio()
        if ratio >= limit and not hypothesis.get("new_evidence"):
            raise HarnessError(
                f"duplicate of rejected {earlier['hypothesis_id']} (similarity {ratio:.2f}); "
                "add new_evidence or change the approach"
            )
    hypothesis_id = f"H-{len(state['hypotheses']) + 1:04d}"
    _append(
        home, state, "hypothesis_registered", {"hypothesis_id": hypothesis_id, **hypothesis}, now
    )
    return hypothesis_id


def experiment_start(home, run_id, hypothesis_id, now):
    state = _active(load(home, run_id))
    step = next_action(state)
    if step["action"] != "execute":
        raise HarnessError(f"cannot start an experiment now; next action is {step}")
    if state["hypotheses"].get(hypothesis_id, {}).get("status") != "registered":
        raise HarnessError(f"{hypothesis_id} is not a registered, untried hypothesis")
    assert_criteria(state)
    data = {"hypothesis_id": hypothesis_id, "snapshot": guard.snapshot(state["project"])}
    _append(home, state, "experiment_started", data, now)
    return data


def _protected(spec):
    return spec["evaluation"].get("files", []) + spec.get("guard", {}).get("protected_paths", [])


def experiment_finish(
    home, run_id, hypothesis_id, now, cost=0.0, downgrade_reason=None, queue=None
):
    """Detect own changes, guard, measure, judge, revert a failure, record the decision."""
    state = _active(load(home, run_id))
    opened = state["open"]
    if not opened or opened["hypothesis_id"] != hypothesis_id:
        raise HarnessError(f"{hypothesis_id} has no open experiment")
    spec, project = state["spec"], state["project"]
    own, violations = guard.changes(project, opened["snapshot"], _protected(spec))
    if not violations and not _criteria_intact(state):
        # Changed where git does not look (an ignored evaluator file). Close the experiment as
        # tampering instead of raising, or the run would be stuck on an experiment it cannot finish.
        violations = [{"kind": "criteria_changed", "path": ""}]
    evaluation_id = f"EV-{state['last_seq'] + 1:04d}"
    if violations or not own:
        checks = []
        measured = {"id": evaluation_id, "purpose": "experiment", "status": "skipped"}
    else:
        checks = guard.run_checks(spec, project)
        measured = evaluation.measure(spec, project, evaluation_id, "experiment", now)
    verdict, reasons, measurements = evaluation.judge(
        spec, state["best"], measured, checks, violations
    )
    override = None
    if downgrade_reason and verdict == "adopt":
        # The agent may only make the outcome more conservative, never less.
        override = {"from": "adopt", "reason": downgrade_reason}
        verdict, reasons = "reject", ["agent_downgrade"]
    patch = f"patches/{hypothesis_id}.patch"
    digest = guard.save_patch(project, own, run_dir(home, run_id) / patch)
    reverted = verdict in ("reject", "unevaluable")
    if reverted:
        guard.revert(project, own)
    decision = {
        "hypothesis_id": hypothesis_id,
        "verdict": verdict,
        "reasons": reasons,
        "signature": "|".join(reasons),
        "measurements": measurements,
        "evaluation": measured,
        "guard": checks,
        "diff": {"patch": patch, "sha256": digest, "files": own},
        "violations": violations,
        "commands": _spec_commands(spec),
        "repro": {"head": opened["snapshot"]["head"], "criteria_hash": state["criteria_hash"]},
        "cost": cost,
        "duration_seconds": max((now - parse_time(opened["started_at"])).total_seconds(), 0.0),
        "reverted": reverted,
        "override": override,
    }
    _append(home, state, "decision_recorded", decision, now)
    if queue is not None:
        enqueue_due_audits(home, queue, now, only=[run_id])
    return decision


def list_runs(home):
    root = Path(home) / "runs"
    if not root.is_dir():
        return []
    return sorted(path.name for path in root.iterdir() if (path / "ledger.jsonl").exists())


def enqueue_due_audits(home, queue, now, only=None, project=None):
    """Queue one audit job per run whose next action is an audit. Idempotent by job id."""
    queued = []
    for run_id in only or list_runs(home):
        try:
            state = load(home, run_id)
        except HarnessError:
            continue  # a corrupt ledger is reported by `status`; it must not hide the other runs
        if project and not Path(project).is_relative_to(state["project"]):
            continue
        step = next_action(state)
        if step["action"] != "audit":
            continue
        job_id = f"audit:{run_id}:{state['anchor']}"
        if queue.enqueue(job_id, "audit", {"run_id": run_id, "triggers": step["triggers"]}, now):
            queued.append(job_id)
    return queued


def audit_record(home, run_id, audit, independence, now):
    validate("audit", audit)
    if independence not in (*INDEPENDENT, "same_context"):
        raise HarnessError("independence must be separate_process, subagent or same_context")
    state = _active(load(home, run_id))
    step = next_action(state)
    data = {
        "audit_id": f"A-{len(state['audits']) + 1:04d}",
        "independence": independence,
        "triggers": step.get("triggers", ["manual"]),
        "findings": audit["findings"],
        "limitations": audit["limitations"],
    }
    _append(home, state, "audit_recorded", data, now)
    return data["audit_id"]


def audit_resolve(home, run_id, audit_id, finding_id, status, evidence, now):
    """An audit finding is a claim too: it is confirmed or refuted with evidence, not obeyed."""
    state = _active(load(home, run_id))
    audit = next((a for a in state["audits"] if a["audit_id"] == audit_id), None)
    if not audit or finding_id not in audit["findings"]:
        raise HarnessError(f"unknown finding: {audit_id}/{finding_id}")
    if status not in RESOLUTIONS:
        raise HarnessError(f"status must be one of {', '.join(RESOLUTIONS)}")
    if not (evidence or "").strip():
        raise HarnessError("resolving a finding needs evidence")
    data = {"audit_id": audit_id, "finding_id": finding_id, "status": status, "evidence": evidence}
    _append(home, state, "finding_resolved", data, now)


def criteria_change(home, run_id, spec, reviewer, reason, now):
    """Only a person changes what counts as passing. The old baseline stops applying."""
    approval = require_human(reviewer, reason)
    state = _active(load(home, run_id))
    if state["open"]:
        raise HarnessError("finish the open experiment before changing criteria")
    _checked_spec(spec)
    data = {
        "spec": spec,
        "criteria_hash": evaluation.criteria_hash(spec, state["project"]),
        "previous_criteria_hash": state["criteria_hash"],
        "approval": approval,
    }
    _append(home, state, "criteria_changed", data, now)


def unblock(home, run_id, reviewer, reason, now):
    approval = require_human(reviewer, reason)
    state = _active(load(home, run_id))
    if not state["blocked"]:
        raise HarnessError("run is not blocked")
    _append(home, state, "unblocked", {"approval": approval}, now)


def stop(home, run_id, reason, note, now):
    """Close the run. A computed outcome must be the one the ledger supports."""
    state = _active(load(home, run_id))
    step = next_action(state)
    if reason in COMPUTED_STOPS:
        if step["action"] != "stop" or step["reason"] != reason:
            raise HarnessError(
                f"the ledger does not support stopping as {reason}; next action is {step}"
            )
    elif reason not in DECLARED_STOPS:
        raise HarnessError(f"unknown stop reason: {reason}")
    data = {"reason": reason, "note": note or step.get("detail", ""), "passed": reason == "passed"}
    _append(home, state, "run_stopped", data, now)
    return data


def status(home, run_id):
    state = load(home, run_id)
    verdicts = Counter(decision["verdict"] for decision in state["decisions"])
    return {
        "run_id": run_id,
        "project": state["project"],
        "objective": state["spec"]["objective"],
        "criteria_hash": state["criteria_hash"],
        "next": next_action(state),
        "passed": bool(state["stopped"] and state["stopped"]["passed"]),
        "iterations": len(state["decisions"]),
        "verdicts": dict(verdicts),
        "cost": state["cost"],
        "seconds": state["seconds"],
        "plateau": plateau_signals(state) if state["baseline"] else [],
    }


def _metric_line(evaluated):
    return ", ".join(f"{name}={m['value']:g}" for name, m in sorted(evaluated["metrics"].items()))


def report(home, run_id):
    """Local Markdown report. An unmet goal is stated as unmet."""
    state = load(home, run_id)
    spec, step = state["spec"], next_action(state)
    if state["stopped"]:
        outcome = "合格" if state["stopped"]["passed"] else f"未達（{state['stopped']['reason']}）"
    else:
        outcome = f"進行中（次の行動: {step['action']}）"
    lines = [
        f"# 改善ループ報告: {run_id}",
        "",
        f"- 目的: {spec['objective']}",
        f"- 結果: {outcome}",
        f"- 評価基準 hash: `{state['criteria_hash']}`",
        f"- 試行: {len(state['decisions'])} / {spec['budget']['max_iterations']}",
        f"- 費用: {state['cost']:g}、実験時間: {state['seconds']:.0f} 秒",
    ]
    if state["baseline"]:
        lines.append(f"- ベースライン: {_metric_line(state['baseline']['evaluation'])}")
        lines.append(f"- 現在の最良: {_metric_line(state['best']['evaluation'])}")
    lines += [
        "",
        "## 仮説台帳",
        "",
        "| ID | 仮説 | 反証条件 | 採否 | 理由 | 差分 |",
        "|---|---|---|---|---|---|",
    ]
    decisions = {decision["hypothesis_id"]: decision for decision in state["decisions"]}
    for hypothesis_id, hypothesis in state["hypotheses"].items():
        decision = decisions.get(hypothesis_id, {})
        cells = [
            hypothesis_id,
            hypothesis["statement"],
            hypothesis["falsification"],
            hypothesis["status"],
            ", ".join(decision.get("reasons", [])) or "-",
            decision.get("diff", {}).get("patch", "-"),
        ]
        lines.append(
            "| " + " | ".join(cell.replace("|", "/").replace("\n", " ") for cell in cells) + " |"
        )
    lines += ["", "## 監査", ""]
    for audit in state["audits"]:
        lines.append(
            f"- {audit['audit_id']}（{audit['independence']}、契機: {', '.join(audit['triggers'])}）"
        )
        for finding in audit["findings"].values():
            lines.append(
                f"  - {finding['id']} [{finding['severity']}/{finding['status']}] {finding['claim']}"
            )
        lines += [f"  - 限界: {limit}" for limit in audit["limitations"]]
    if not state["audits"]:
        lines.append("- 監査は未実施")
    return "\n".join(lines) + "\n"
