"""`iac validate`: check one delivery record and classify one plan. Nothing is applied.

Reads the user's files (the plan, the evidence directory, the delivery record) and writes two
files into a new run: `risk.json` and `transition-check.json`. Input that cannot be read is
an InputError before any run exists; contract violations are recorded, then exit 1.
"""

import json
from pathlib import Path

from .. import guard, paths, records
from ..errors import EXIT_OK, EXIT_QUALITY, InputError, QualityGateError
from . import contract, risk

MAX_PLAN_BYTES = guard.OPA_MAX_INPUT_BYTES
MAX_DELIVERY_BYTES = 2 * 1024 * 1024
LEVELS = ("high", "medium", "low", "none")


def _read(path, limit, what):
    path = Path(path)
    if not path.is_file():
        raise InputError(f"{what} is not a file: {path}")
    if path.stat().st_size > limit:
        raise QualityGateError(f"{what} is over {limit} bytes; stopped instead of cutting it.")
    data = path.read_bytes()
    try:
        return data, json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise InputError(f"{what} is not valid JSON: {error}") from None
    except RecursionError:
        raise InputError(f"{what} is nested too deep to read as JSON.") from None


def _plan(path):
    data, plan = _read(path, MAX_PLAN_BYTES, "--plan")
    problems = records.violations("iac-delivery", plan, "plan")
    if problems:
        raise InputError("--plan is not a plan with resource_changes: " + "; ".join(problems[:3]))
    return data, plan


def validate(root, plan_path, evidence_dir, delivery_path=None, env=None):
    """(transition check, risk record). Both are written to a new run under `root`."""
    plan_bytes, plan = _plan(plan_path)
    evidence_dir = Path(evidence_dir)
    if not evidence_dir.is_dir():
        raise InputError(f"--evidence is not a directory: {evidence_dir}")
    plan_sha256 = records.sha256(plan_bytes)
    delivery, delivery_sha256 = None, None
    if delivery_path is not None:
        delivery_bytes, delivery = _read(delivery_path, MAX_DELIVERY_BYTES, "--delivery")
        delivery_sha256 = records.sha256(delivery_bytes)
    found = [] if delivery is None else contract.check(delivery, evidence_dir, plan_sha256)
    bound = delivery is not None and contract.delivered_plan(delivery) == plan_sha256
    classified = risk.classify(plan, env)

    run_dir = paths.create_run(root)
    head = {"schema_version": records.SCHEMA_VERSION, "run_id": run_dir.name}
    head["created_at"] = records.iso()
    shaped = isinstance(delivery, dict)
    transitions = delivery.get("transitions") if shaped else None
    check = {
        **head,
        "status": "not_checked" if delivery is None else ("violations" if found else "valid"),
        "change_id": _text(delivery.get("change_id")) if shaped else None,
        "state": _text(delivery.get("state")) if shaped else None,
        "delivery_sha256": delivery_sha256,
        "plan_sha256": plan_sha256,
        "transitions_checked": len(transitions) if isinstance(transitions, list) else 0,
        "violations": [
            {"id": records.record_id("IV", number), **item}
            for number, item in enumerate(found, start=1)
        ],
    }
    risk_record = {**head, "plan_sha256": plan_sha256, "bound_to_delivery": bound, **classified}
    records.validate("iac-delivery", check, "transition_check")
    records.validate("iac-delivery", risk_record, "risk")
    records.write_json(paths.child(run_dir, "transition-check.json"), check)
    records.write_json(paths.child(run_dir, "risk.json"), risk_record)
    return check, risk_record


def _text(value):
    return value if isinstance(value, str) else None


def exit_code(check):
    return EXIT_QUALITY if check["status"] == "violations" else EXIT_OK


def summary(check, risk_record):
    counts = {}
    for item in check["violations"]:
        counts[item["code"]] = counts.get(item["code"], 0) + 1
    return {
        "run_id": check["run_id"],
        "status": check["status"],
        "violations": counts,
        "risk": risk_record.get("level", risk_record["status"]),
        "bound_to_delivery": risk_record["bound_to_delivery"],
        "files": ["risk.json", "transition-check.json"],
    }


def evaluator_output(check, risk_record):
    """The v2 evaluator adapter shape (`evaluation.schema.json#/$defs/output`)."""
    metrics, evidence, notes = {}, [], []
    if check["status"] == "not_checked":
        notes.append("No --delivery was given; no transition was checked.")
    else:
        metrics["contract_violations"] = float(len(check["violations"]))
        metrics["transitions_checked"] = float(check["transitions_checked"])
        evidence += [
            {
                "id": item["id"],
                "ref": f"transition-check.json#{item['id']}",
                "note": f"{item['code']}: {item['message']}",
            }
            for item in check["violations"]
        ]
        notes.append("The delivery contract is checked by fixed rules; no model is asked.")
        if not risk_record["bound_to_delivery"]:
            notes.append(
                "The plan is not bound to the delivery: it records no plan yet, or another one."
            )
    if risk_record["status"] == "evaluated":
        levels = [item["level"] for item in risk_record["changes"]]
        for level in LEVELS:
            metrics[f"risk_{level}"] = float(levels.count(level))
        metrics["risk_changes_total"] = float(len(levels))
        evidence.append({"id": "risk", "ref": "risk.json", "note": f"level {risk_record['level']}"})
        notes.append("Risk levels come from the bundled Rego sample, not a complete policy.")
    else:
        notes.append(f"The plan risk is unevaluable: {risk_record['unevaluable_reason']}")
    output = {"metrics": metrics, "evidence": evidence, "notes": " ".join(notes)}
    if not metrics:
        output["unevaluable"] = (
            "nothing was checked: no --delivery, and " + risk_record["unevaluable_reason"]
        )
    return output
