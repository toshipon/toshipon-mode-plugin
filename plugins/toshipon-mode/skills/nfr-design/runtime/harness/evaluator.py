"""Deterministic consistency checks, not proof of deployed AWS behavior."""

import json
import math
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[2]
METRICS = {
    "availability_percent": ("availability", "percent"),
    "p95_latency_ms": ("performance", "ms"),
    "rto_minutes": ("recovery", "minutes"),
    "rpo_minutes": ("recovery", "minutes"),
    "encryption_percent": ("security", "percent"),
    "detection_minutes": ("operations", "minutes"),
    "monthly_cost_usd": ("cost", "USD/month"),
}
LIMITATIONS = [
    "Document quality only. Load tests, recovery drills, cost estimates and business approval are separate.",
    "Evidence verification is supplied by the caller. This harness does not fetch or verify URL contents.",
]


def schema(name):
    return json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text(encoding="utf-8"))


def finite_numbers(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(finite_numbers(v) for v in value.values())
    if isinstance(value, list):
        return all(finite_numbers(v) for v in value)
    return True


def evaluate(input_data, design, review):
    findings = []

    def add(code, description, severity="high"):
        findings.append({"id": code, "severity": severity, "description": description})

    for name, value in [("input", input_data), ("design", design), ("review", review)]:
        if not finite_numbers(value):
            add("NON_FINITE", f"{name} contains non-finite numeric values.")
        for error in Draft202012Validator(schema(name)).iter_errors(value):
            add("SCHEMA", f"{name}/{'/'.join(map(str, error.path))}: {error.message}")
    if findings:
        return {"passed": False, "findings": findings, "limitations": LIMITATIONS.copy()}

    groups = {
        "requirements": design["requirements"],
        "decisions": design["decisions"],
        "evidence": design["evidence"],
        "verifications": design["verifications"],
        "open_issues": design["open_issues"],
        "input requirements": input_data["requirements"],
        "input evidence": input_data["technical_evidence"],
        "review findings": review["findings"],
    }
    for name, values in groups.items():
        ids = [v["id"] for v in values]
        if len(ids) != len(set(ids)):
            add("DUPLICATE_ID", f"Duplicate IDs in {name}.")
    original = {r["id"]: r for r in input_data["requirements"]}
    proposed = {r["id"]: r for r in design["requirements"]}
    if original != proposed:
        add("INPUT_INTEGRITY", "Requirements must exactly preserve the input snapshot.")
    evidence = {e["id"]: e for e in design["evidence"]}
    approved_evidence = {e["id"]: e for e in input_data["technical_evidence"]}
    if evidence != approved_evidence:
        add(
            "EVIDENCE_INTEGRITY",
            "Design evidence must exactly preserve the caller-reviewed evidence registry.",
        )
    covered = set()
    for decision in design["decisions"]:
        for rid in decision["requirement_ids"]:
            if rid not in original:
                add("DANGLING_REQUIREMENT", f"{decision['id']} references {rid}.")
            covered.add(rid)
        for eid in decision["evidence_ids"]:
            if eid not in evidence:
                add("DANGLING_EVIDENCE", f"{decision['id']} references {eid}.")
    for item in evidence.values():
        try:
            url = urlparse(item["url"])
            valid_url = (
                url.scheme == "https"
                and url.hostname in {"docs.aws.amazon.com", "aws.amazon.com"}
                and not url.username
                and not url.password
                and url.port in {None, 443}
            )
        except ValueError:
            valid_url = False
        try:
            date.fromisoformat(item["checked_at"])
            valid_date = True
        except ValueError:
            valid_date = False
        if (
            item["status"] != "verified"
            or not item["verified_by"].strip()
            or not valid_url
            or not valid_date
        ):
            add(
                "EVIDENCE",
                f"{item['id']} needs caller verification, reviewer, date and an official AWS HTTPS URL.",
            )
    tested = set()
    for verification in design["verifications"]:
        rid = verification["requirement_id"]
        if rid not in original:
            add("DANGLING_VERIFICATION", f"{verification['id']} references {rid}.")
            continue
        tested.add(rid)
        requirement = original[rid]
        if (
            verification["target"] != requirement["target"]
            or verification["unit"] != requirement["unit"]
        ):
            add("ACCEPTANCE_DRIFT", f"{verification['id']} target/unit differs from the input.")
    for rid, requirement in original.items():
        if rid not in covered:
            add("MISSING_DESIGN", f"{rid} has no design decision.")
        if rid not in tested:
            add("MISSING_VERIFICATION", f"{rid} has no verification plan.")
        metric = requirement["metric"]
        category, unit = METRICS[metric]
        if requirement["category"] != category or requirement["unit"] != unit:
            add("METRIC_UNIT", f"{rid} requires category {category} and unit {unit}.")
        target = requirement["target"]
        if target <= 0 and metric != "rpo_minutes":
            add("TARGET", f"{rid} target must be greater than zero.")
        if metric in {"availability_percent", "encryption_percent"} and target > 100:
            add("TARGET", f"{rid} percent target must not exceed 100.")
        if metric in {"rto_minutes", "rpo_minutes"} and design["aws"][metric] > target:
            add("RECOVERY_TARGET", f"{metric} exceeds {rid}.")
        if metric == "monthly_cost_usd" and design["aws"]["monthly_cost_usd"] > target:
            add("COST_TARGET", f"Monthly estimate exceeds {rid}.")
        if metric == "availability_percent" and target >= 99.9 and design["aws"]["az_count"] < 2:
            add("AZ_POLICY", "MVP policy requires at least two AZs at >=99.9% availability.")
    if design["aws"]["monthly_cost_usd"] > input_data["budget_monthly_usd"]:
        add("BUDGET", "Monthly estimate exceeds the input budget.")
    for issue in design["open_issues"]:
        if issue["status"] == "open":
            add("OPEN_ISSUE", f"{issue['id']}: {issue['description']}", issue["severity"])
    for item in review["findings"]:
        add("AUDIT_" + item["id"], item["description"], item["severity"])
    return {
        "passed": not any(f["severity"] in {"critical", "high"} for f in findings),
        "findings": findings,
        "limitations": review["limitations"] + LIMITATIONS,
    }
