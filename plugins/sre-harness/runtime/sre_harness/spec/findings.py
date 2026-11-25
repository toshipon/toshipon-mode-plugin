"""Findings: their kinds, severities, the inferred cap, ids and the invariants they keep.

Severity is fixed per kind. A finding is `basis: explicit` when at least one piece of its
evidence is explicit (a rule read it from a file, the registry or the API); otherwise it is
`inferred` and its severity is capped at `medium`, so an inferred trace link is never the only
ground of a `critical` or `high` finding.
"""

from .. import records

SEVERITIES = ("critical", "high", "medium", "low", "info")
SEVERITY = {
    "semantic_conflict": "critical",
    "contradiction": "high",
    "superseded_dependency": "high",
    "missing_test": "high",
    "stale_sha": "high",
    "not_reflected": "medium",
    "ambiguous_requirement": "medium",
    "input_missing": "medium",
    "impact": "info",
}
KINDS = tuple(SEVERITY)
INFERRED_CAP = "medium"


def cap(severity, basis):
    if basis == "inferred" and SEVERITIES.index(severity) < SEVERITIES.index(INFERRED_CAP):
        return INFERRED_CAP
    return severity


def evidence(ref, basis, note=""):
    item = {"ref": ref, "basis": basis}
    if note:
        item["note"] = note[:500]
    return item


def draft(kind, entities, prs, items, explanation, **extra):
    """A finding without an id. Severity and basis follow from the kind and the evidence."""
    basis = "explicit" if any(item["basis"] == "explicit" for item in items) else "inferred"
    numbered = [{"id": f"E{index}", **item} for index, item in enumerate(items, start=1)]
    return {
        "kind": kind,
        "severity": cap(SEVERITY[kind], basis),
        "entities": sorted(set(entities)),
        "prs": sorted(set(prs)),
        "evidence": numbered,
        "basis": basis,
        "explanation": explanation[:2000],
        **extra,
    }


def numbered(drafts):
    """Sorted and given ids FD-0001, ... The order is a function of the content only."""
    ordered = sorted(
        drafts,
        key=lambda item: (
            KINDS.index(item["kind"]),
            item["prs"],
            item["entities"],
            records.canonical(item["evidence"]),
        ),
    )
    return [
        {"id": records.record_id("FD", number), **item}
        for number, item in enumerate(ordered, start=1)
    ]


def check(findings):
    """Invariant violations, empty when the findings hold."""
    problems, seen = [], set()
    for item in findings:
        name = item.get("id", "?")
        if name in seen:
            problems.append(f"{name}: the id is used twice")
        seen.add(name)
        explicit = any(entry["basis"] == "explicit" for entry in item["evidence"])
        if item["basis"] == "explicit" and not explicit:
            problems.append(f"{name}: basis explicit without explicit evidence")
        if item["basis"] == "inferred" and explicit:
            problems.append(f"{name}: basis inferred although explicit evidence exists")
        if item["basis"] == "inferred" and SEVERITIES.index(item["severity"]) < SEVERITIES.index(
            INFERRED_CAP
        ):
            problems.append(f"{name}: {item['severity']} rests on inferred evidence only")
        if item["kind"] in ("semantic_conflict",) and len(item["prs"]) < 2:
            problems.append(f"{name}: a semantic conflict needs two PRs")
        if item["kind"] in ("missing_test", "stale_sha", "impact") and len(item["prs"]) != 1:
            problems.append(f"{name}: {item['kind']} belongs to exactly one PR")
    return problems
