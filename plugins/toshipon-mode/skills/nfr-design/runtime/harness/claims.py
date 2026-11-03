"""Trace source quotations into reviewed requirements and change impact.

Only the explicit review command writes approval fields. Extraction cannot approve
its own output. Review history binds the exact claim content but is not an identity
or signature system; an authorized operator still controls the local workspace.
"""

import copy
import hashlib
import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .evaluator import METRICS

ROOT = Path(__file__).resolve().parents[2]
MAX_EXTRACTION_BYTES = 1024 * 1024
EXTRACTED_FIELDS = (
    "id",
    "kind",
    "subject",
    "metric",
    "value",
    "unit",
    "scope",
    "period",
    "summary",
    "source_refs",
)


class ClaimsError(ValueError):
    """Source claims are invalid, unresolved or have no reviewed requirements."""


def _schema(name):
    return _load(ROOT / "schemas" / f"{name}.schema.json")


def _load(path):
    def reject(value):
        raise ClaimsError(f"Non-finite JSON value {value} is not allowed.")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ClaimsError("Duplicate JSON object keys are not allowed.")
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding="utf-8"), parse_constant=reject, object_pairs_hook=pairs
    )


def _bytes(data):
    return (
        json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode("utf-8")


def _digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def claim_digest(claim):
    material = {key: claim[key] for key in EXTRACTED_FIELDS}
    return hashlib.sha256(_bytes(material)).hexdigest()


def _context(workspace):
    from .intake import load_context

    return load_context(workspace)


def _claims_path(workspace):
    path = Path(workspace) / "claims.json"
    if path.is_symlink():
        raise ClaimsError("claims.json must be a regular local file.")
    return path


def source_fingerprints(claim, context, claims=None):
    sources = {source["id"]: source for source in context["sources"]}
    by_id = {item["id"]: item for item in (claims or [claim])}
    pending, seen, source_ids = [claim["id"]], set(), set()
    while pending:
        cid = pending.pop()
        if cid in seen or cid not in by_id:
            continue
        seen.add(cid)
        item = by_id[cid]
        source_ids.update(ref["source_id"] for ref in item["source_refs"])
        pending.extend(item["supersedes"])
    return {
        sid: {
            key: sources[sid][key]
            for key in ("version", "locator", "owner", "content_sha256", "kind", "title")
        }
        for sid in sorted(source_ids)
        if sid in sources
    }


def _group(claim):
    return tuple(claim[key] for key in ("subject", "metric", "scope", "period"))


def _finite(value):
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, dict):
        return all(_finite(v) for v in value.values())
    if isinstance(value, list):
        return all(_finite(v) for v in value)
    return True


def _review_time(value):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _active(claims):
    superseded = {
        cid
        for claim in claims
        if claim["approval"]["status"] == "approved"
        for cid in claim["supersedes"]
    }
    return [
        c for c in claims if c["approval"]["status"] != "rejected" and c["id"] not in superseded
    ]


def validate_claims(workspace, claims_data, context=None):
    """Return fail-closed findings for schemas, quotations, reviews and supersession."""
    findings = []

    def add(code, description, claim_ids=None, source_ids=None):
        item = {"id": code, "severity": "high", "description": description}
        if claim_ids:
            item["claim_ids"] = claim_ids
        if source_ids:
            item["source_ids"] = source_ids
        findings.append(item)

    if not _finite(claims_data):
        add("CLAIM_NON_FINITE", "Claims contain non-finite values.")
    for error in Draft202012Validator(_schema("claims")).iter_errors(claims_data):
        add("CLAIM_SCHEMA", f"claims/{'/'.join(map(str, error.path))}: {error.message}")
    if findings:
        return findings
    context = _context(workspace) if context is None else context
    sources = {s["id"]: s for s in context["sources"]}
    claims = claims_data["claims"]
    by_id = {c["id"]: c for c in claims}
    if len(by_id) != len(claims):
        add("CLAIM_DUPLICATE", "Claim IDs must be unique.")
    texts = {}
    for sid, source in sources.items():
        snapshot = Path(workspace) / source["snapshot_path"]
        if snapshot.is_symlink() or not snapshot.is_file():
            add("CLAIM_SOURCE", f"Source snapshot {sid} is not a regular file.", source_ids=[sid])
            texts[sid] = ""
            continue
        texts[sid] = snapshot.read_text(encoding="utf-8")
        if _digest(snapshot) != source["content_sha256"]:
            add("CLAIM_STALE", f"Source snapshot {sid} changed after collection.", source_ids=[sid])
    for claim in claims:
        cid = claim["id"]
        for ref in claim["source_refs"]:
            sid = ref["source_id"]
            source = sources.get(sid)
            if source is None:
                add("CLAIM_SOURCE", f"{cid} references unknown source {sid}.", [cid], [sid])
            elif ref["content_sha256"] != source["content_sha256"]:
                add(
                    "CLAIM_STALE",
                    f"{cid} was extracted from a different version of {sid}.",
                    [cid],
                    [sid],
                )
            elif not ref["quote"].strip() or ref["quote"] not in texts[sid]:
                add(
                    "CLAIM_QUOTE",
                    f"{cid} quotation is absent from the snapshot of {sid}.",
                    [cid],
                    [sid],
                )
        approval = claim["approval"]
        history = claim["review_history"]
        status = approval["status"]
        if status == "unreviewed":
            if (
                history
                or claim["requirement_id"] is not None
                or claim["supersedes"]
                or any(approval[k] for k in ("reviewer", "reviewed_at", "reason"))
            ):
                add("CLAIM_REVIEW", f"{cid} has review fields before explicit review.", [cid])
        else:
            if (
                not approval["reviewer"].strip()
                or not approval["reason"].strip()
                or not _review_time(approval["reviewed_at"])
                or not history
            ):
                add(
                    "CLAIM_REVIEW",
                    f"{cid} needs a named reviewer, date, reason and history.",
                    [cid],
                )
            if history:
                last = history[-1]
                expected = {
                    **approval,
                    "requirement_id": claim["requirement_id"],
                    "supersedes": claim["supersedes"],
                    "claim_sha256": claim_digest(claim),
                    "source_fingerprints": last["source_fingerprints"],
                }
                if last["source_fingerprints"] != source_fingerprints(claim, context, claims):
                    add(
                        "CLAIM_REVIEW_STALE",
                        f"{cid} review predates source version or authority changes.",
                        [cid],
                    )
                if last != expected:
                    add("CLAIM_REVIEW", f"{cid} changed after its last review.", [cid])
                for record in history:
                    if (
                        not record["reviewer"].strip()
                        or not record["reason"].strip()
                        or not _review_time(record["reviewed_at"])
                        or record["claim_sha256"] != claim_digest(claim)
                    ):
                        add("CLAIM_HISTORY", f"{cid} has invalid review history.", [cid])
                        break
        if status == "approved":
            if claim["kind"] not in {"requirement", "decision", "observed"}:
                add("CLAIM_KIND", f"{cid} cannot be approved as {claim['kind']}.", [cid])
            if claim["kind"] == "requirement" and claim["requirement_id"] is None:
                add("CLAIM_REQUIREMENT_ID", f"{cid} needs a stable requirement ID.", [cid])
        if claim["requirement_id"] is not None and claim["kind"] != "requirement":
            add("CLAIM_REQUIREMENT_ID", f"{cid} is not a requirement.", [cid])
        for old_id in claim["supersedes"]:
            old = by_id.get(old_id)
            if (
                status != "approved"
                or claim["kind"] not in {"requirement", "decision"}
                or old is None
                or old_id == cid
                or old["kind"] not in {"requirement", "decision"}
                or _group(old) != _group(claim)
            ):
                add("CLAIM_SUPERSESSION", f"{cid} cannot supersede {old_id}.", [cid, old_id])

    # A directed supersession cycle must never make every conflicting claim disappear.
    def cycles(cid, visiting, done):
        if cid in visiting:
            return True
        if cid in done:
            return False
        visiting.add(cid)
        for target in by_id[cid]["supersedes"]:
            if target in by_id and cycles(target, visiting, done):
                return True
        visiting.remove(cid)
        done.add(cid)
        return False

    if any(cycles(cid, set(), set()) for cid in by_id):
        add("CLAIM_SUPERSESSION", "Supersession contains a cycle.")
    active_ids = [
        c["requirement_id"]
        for c in _active(claims)
        if c["kind"] == "requirement" and c["approval"]["status"] == "approved"
    ]
    if len(active_ids) != len(set(active_ids)):
        add("CLAIM_REQUIREMENT_ID", "Active approved requirements share an ID.")
    return findings


def extract_claims(workspace, backend):
    """Extract source-backed candidates; never accept model-supplied review fields."""
    path = _claims_path(workspace)
    if path.exists():
        raise ClaimsError("claims.json already exists; use a new collected workspace.")
    context = _context(workspace)
    hashes = {"context.json": _digest(Path(workspace) / "context.json")}
    sources = []
    total_bytes = 0
    for source in context["sources"]:
        record = copy.deepcopy(source)
        snapshot = Path(workspace) / source["snapshot_path"]
        hashes[source["snapshot_path"]] = _digest(snapshot)
        record["text"] = snapshot.read_text(encoding="utf-8")
        total_bytes += len(record["text"].encode("utf-8"))
        if total_bytes > MAX_EXTRACTION_BYTES:
            raise ClaimsError(
                "Source context exceeds the 1 MiB extraction limit; split the workspace."
            )
        sources.append(record)
    output = backend("extractor", {"sources": sources}, _schema("extraction"))
    try:
        Draft202012Validator(_schema("extraction")).validate(output)
    except ValidationError as error:
        raise ClaimsError("Extractor output does not match the candidate-only schema.") from error
    for rel, expected in hashes.items():
        if _digest(Path(workspace) / rel) != expected:
            raise ClaimsError("Source context changed during extraction.")
    result = {"schema_version": 1, "claims": []}
    for claim in output["claims"]:
        candidate = copy.deepcopy(claim)
        candidate.update(
            {
                "approval": {
                    "status": "unreviewed",
                    "reviewer": "",
                    "reviewed_at": "",
                    "reason": "",
                },
                "requirement_id": None,
                "supersedes": [],
                "review_history": [],
            }
        )
        result["claims"].append(candidate)
    _context(workspace)
    findings = validate_claims(workspace, result, context)
    if findings:
        raise ClaimsError("Extracted claims failed source quotation or structure validation.")
    with path.open("xb") as stream:
        stream.write(_bytes(result))
    return result


def assess_context(workspace, claims_data=None):
    claims_data = _load(_claims_path(workspace)) if claims_data is None else claims_data
    context = _context(workspace)
    findings = validate_claims(workspace, claims_data, context)
    if findings:
        return {"passed": False, "findings": findings, "conflicts": [], "gaps": []}
    active = _active(claims_data["claims"])
    desired = defaultdict(list)
    observed = defaultdict(list)
    conflicts, gaps = [], []
    for claim in active:
        if claim["kind"] in {"requirement", "decision"}:
            desired[_group(claim)].append(claim)
            if any(
                claim[key].strip().lower() in {"unknown", "unspecified", "未定", "不明"}
                for key in ("subject", "metric", "scope", "period")
            ):
                findings.append(
                    {
                        "id": "CLAIM_SCOPE_UNRESOLVED",
                        "severity": "high",
                        "description": f"{claim['id']} needs an explicit subject, metric, scope and measurement period.",
                        "claim_ids": [claim["id"]],
                    }
                )
            if claim["approval"]["status"] != "approved":
                findings.append(
                    {
                        "id": "CLAIM_UNREVIEWED",
                        "severity": "high",
                        "description": f"{claim['id']} has not been explicitly approved.",
                        "claim_ids": [claim["id"]],
                    }
                )
        elif claim["kind"] == "observed":
            observed[_group(claim)].append(claim)
        elif claim["kind"] in {"proposal", "unknown"}:
            findings.append(
                {
                    "id": "CLAIM_UNRESOLVED",
                    "severity": "high" if claim["kind"] == "unknown" else "medium",
                    "description": f"{claim['id']} remains {claim['kind']}; it is not a requirement.",
                    "claim_ids": [claim["id"]],
                }
            )
    for group, group_claims in desired.items():
        alternatives = {(json.dumps(c["value"], sort_keys=True), c["unit"]) for c in group_claims}
        if len(alternatives) > 1:
            ids = [c["id"] for c in group_claims]
            conflict = {
                "id": "CLAIM_CONFLICT",
                "severity": "high",
                "description": f"Conflicting values for {group[0]} / {group[1]} in scope {group[2]} and period {group[3]}.",
                "claim_ids": ids,
                "source_ids": sorted(
                    {r["source_id"] for c in group_claims for r in c["source_refs"]}
                ),
            }
            conflicts.append(conflict)
            findings.append(copy.deepcopy(conflict))
        for current in observed.get(group, []):
            if any(
                current["value"] != wanted["value"] or current["unit"] != wanted["unit"]
                for wanted in group_claims
            ):
                gap = {
                    "id": "CONTEXT_GAP",
                    "severity": "medium",
                    "description": f"Observed {current['id']} differs from the desired value; the observation is not an approved target.",
                    "claim_ids": [current["id"]] + [c["id"] for c in group_claims],
                    "source_ids": sorted(
                        {r["source_id"] for c in [current, *group_claims] for r in c["source_refs"]}
                    ),
                }
                gaps.append(gap)
                findings.append(copy.deepcopy(gap))
    return {
        "passed": not any(f["severity"] in {"critical", "high"} for f in findings),
        "findings": findings,
        "conflicts": conflicts,
        "gaps": gaps,
    }


def review_claim(
    workspace, claim_id, status, reviewer, reason, requirement_id=None, supersedes=None
):
    """Record an explicit operator decision, retaining every previous review."""
    if status not in {"approved", "rejected"} or not reviewer.strip() or not reason.strip():
        raise ClaimsError("Review requires approved/rejected status, reviewer and reason.")
    path = _claims_path(workspace)
    before = path.read_bytes()
    data = _load(path)
    context = _context(workspace)
    context_hash = _digest(Path(workspace) / "context.json")
    existing = [
        f for f in validate_claims(workspace, data, context) if f["id"] != "CLAIM_REVIEW_STALE"
    ]
    if existing:
        raise ClaimsError(
            "Existing claims failed validation; do not review stale or altered claims."
        )
    claim = next((c for c in data["claims"] if c["id"] == claim_id), None)
    if claim is None:
        raise ClaimsError("Unknown claim ID.")
    if claim["requirement_id"] is not None:
        if requirement_id is not None and requirement_id != claim["requirement_id"]:
            raise ClaimsError("An assigned requirement ID cannot be renumbered.")
        requirement_id = claim["requirement_id"]
    approved_at = datetime.now(timezone.utc).isoformat()
    claim["approval"] = {
        "status": status,
        "reviewer": reviewer,
        "reviewed_at": approved_at,
        "reason": reason,
    }
    claim["requirement_id"] = requirement_id
    claim["supersedes"] = (
        list(claim["supersedes"] if supersedes is None else supersedes)
        if status == "approved"
        else []
    )
    claim["review_history"].append(
        {
            **claim["approval"],
            "requirement_id": requirement_id,
            "supersedes": claim["supersedes"],
            "claim_sha256": claim_digest(claim),
            "source_fingerprints": source_fingerprints(claim, context, data["claims"]),
        }
    )
    if any(f["id"] != "CLAIM_REVIEW_STALE" for f in validate_claims(workspace, data)):
        raise ClaimsError("Review violates claim, requirement ID or supersession rules.")
    if _digest(Path(workspace) / "context.json") != context_hash:
        raise ClaimsError("Source context changed during review.")
    if path.read_bytes() != before:
        raise ClaimsError("Claims changed during review.")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(_bytes(data))
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return data


def requirements_from_claims(claims_data):
    """Project active approved requirements without inventing targets or dropping scope."""
    requirements = []
    for claim in _active(claims_data["claims"]):
        if claim["kind"] != "requirement" or claim["approval"]["status"] != "approved":
            continue
        metric = claim["metric"]
        if metric not in METRICS:
            raise ClaimsError(f"Unsupported requirement metric {metric}.")
        category, unit = METRICS[metric]
        value = claim["value"]
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or claim["unit"] != unit
        ):
            raise ClaimsError(f"{claim['id']} needs a numeric value and exact unit {unit}.")
        if (
            value < 0
            or (value == 0 and metric != "rpo_minutes")
            or (unit == "percent" and value > 100)
        ):
            raise ClaimsError(f"{claim['id']} target is outside the supported range.")
        requirements.append(
            {
                "id": claim["requirement_id"],
                "category": category,
                "metric": metric,
                "target": value,
                "unit": unit,
                "description": (
                    f"{claim['summary']} 対象 {claim['subject']}。"
                    f"範囲 {claim['scope']}。測定期間 {claim['period']}。"
                ),
                "source_refs": sorted({r["source_id"] for r in claim["source_refs"]}),
                "context_refs": [claim["id"]],
            }
        )
    if not requirements:
        raise ClaimsError("There are no active approved requirements.")
    return requirements


def prepare_input(workspace, evidence_path, out):
    """Build numeric design input from active, explicitly approved requirements only."""
    context = _context(workspace)
    claims_path = _claims_path(workspace)
    before_context = _digest(Path(workspace) / "context.json")
    before_claims = _digest(claims_path)
    claims_data = _load(claims_path)
    assessment = assess_context(workspace, claims_data)
    if not assessment["passed"]:
        raise ClaimsError(
            "Resolve context conflicts and unreviewed requirements before preparing input."
        )
    requirements = requirements_from_claims(claims_data)
    evidence = _load(evidence_path)
    if not isinstance(evidence, list):
        raise ClaimsError("Technical evidence must be a JSON array supplied by the caller.")
    result = {
        "project": context["project"],
        "region": context["region"],
        "budget_monthly_usd": context["budget_monthly_usd"],
        "technical_evidence": evidence,
        "requirements": requirements,
        "context_binding": {
            "context_sha256": _digest(Path(workspace) / "context.json"),
            "claims_sha256": _digest(claims_path),
        },
    }
    if (
        _digest(Path(workspace) / "context.json") != before_context
        or _digest(claims_path) != before_claims
    ):
        raise ClaimsError("Context or reviewed claims changed during input preparation.")
    with Path(out).open("xb") as stream:
        stream.write(_bytes(result))
    return result


def diff_context(before, after, input_data, design=None):
    """Map changed context conservatively; a candidate impact is not proof of causality."""
    old = _context(before)
    new = _context(after)
    if not isinstance(input_data, dict) or not isinstance(input_data.get("requirements"), list):
        raise ClaimsError("Impact analysis requires a structured requirement input.")
    bound = "context_binding" in input_data
    for field in ("project", "region", "budget_monthly_usd"):
        if (bound and field not in input_data) or (
            field in input_data and input_data[field] != old[field]
        ):
            raise ClaimsError("Input project, region and budget must match the before context.")
    claims_path = _claims_path(before)
    old_data = _load(claims_path) if claims_path.exists() else None
    if old_data is not None and (
        not isinstance(old_data, dict) or not isinstance(old_data.get("claims"), list)
    ):
        raise ClaimsError("The before claims snapshot is malformed.")
    if old_data is not None and any(Draft202012Validator(_schema("claims")).iter_errors(old_data)):
        raise ClaimsError("The before claims snapshot does not match the claims schema.")
    old_claims = old_data["claims"] if old_data is not None else []
    if bound:
        binding = input_data["context_binding"]
        if (
            not isinstance(binding, dict)
            or binding.get("context_sha256") != _digest(Path(before) / "context.json")
            or old_data is None
            or binding.get("claims_sha256") != _digest(claims_path)
        ):
            raise ClaimsError("Input is not bound to the before context and reviewed claims.")
        if validate_claims(before, old_data, old):
            raise ClaimsError("The before context contains invalid or stale reviewed claims.")
        expected = {r["id"]: r for r in requirements_from_claims(old_data)}
        try:
            proposed = {r["id"]: r for r in input_data["requirements"]}
        except (TypeError, KeyError) as error:
            raise ClaimsError("Impact analysis requires valid requirement records.") from error
        if len(proposed) != len(input_data["requirements"]) or proposed != expected:
            raise ClaimsError("Input requirements differ from the reviewed before context.")
    old_sources = {s["id"]: s for s in old["sources"]}
    new_sources = {s["id"]: s for s in new["sources"]}
    changed = []
    for sid in sorted(old_sources.keys() | new_sources.keys()):
        previous, current = old_sources.get(sid), new_sources.get(sid)
        if previous is None:
            change = "added"
        elif current is None:
            change = "removed"
        elif previous["content_sha256"] != current["content_sha256"]:
            change = "content_changed"
        elif previous["version"] != current["version"]:
            change = "version_changed"
        elif any(previous[k] != current[k] for k in ("locator", "owner", "kind", "title")):
            change = "metadata_changed"
        else:
            continue
        changed.append(
            {
                "id": sid,
                "change": change,
                "before_version": previous["version"] if previous else None,
                "after_version": current["version"] if current else None,
            }
        )
    metadata_changes = [
        {"field": field, "before": old[field], "after": new[field]}
        for field in ("project", "region", "budget_monthly_usd")
        if old[field] != new[field]
    ]
    ids = {s["id"] for s in changed}
    try:
        direct = sorted(
            r["id"] for r in input_data["requirements"] if ids.intersection(r["source_refs"])
        )
        all_requirements = {r["id"] for r in input_data["requirements"]}
    except (TypeError, KeyError) as error:
        raise ClaimsError("Impact analysis requires valid requirement records.") from error
    affected = sorted(all_requirements) if bound and (changed or metadata_changes) else direct
    affected_ids = set(affected)
    candidate = design or {"decisions": [], "verifications": []}
    # Selecting C1 over C2 relies on the context of both. If C2's source changes,
    # C1's previous resolution must be reconsidered even when C1's text is stable.
    by_id = {c["id"]: c for c in old_claims}

    def dependency_sources(claim_id):
        sources, pending, visited = set(), [claim_id], set()
        while pending:
            cid = pending.pop()
            if cid in visited or cid not in by_id:
                continue
            visited.add(cid)
            claim = by_id[cid]
            sources.update(ref["source_id"] for ref in claim["source_refs"])
            pending.extend(claim["supersedes"])
        return sources

    invalidated = sorted(
        c["id"]
        for c in old_claims
        if c["approval"]["status"] == "approved" and ids.intersection(dependency_sources(c["id"]))
    )
    try:
        decisions = sorted(
            d["id"]
            for d in candidate["decisions"]
            if affected_ids.intersection(d["requirement_ids"])
        )
        verifications = sorted(
            v["id"] for v in candidate["verifications"] if v["requirement_id"] in affected_ids
        )
    except (TypeError, KeyError) as error:
        raise ClaimsError(
            "Impact analysis requires valid design and verification references."
        ) from error
    return {
        "changed_sources": changed,
        "context_metadata_changes": metadata_changes,
        "affected_requirements": affected,
        "directly_affected_requirements": direct,
        "potentially_affected_requirements": sorted(affected_ids - set(direct)),
        "impact_mode": "conservative_context" if bound else "direct_sources",
        "affected_decisions": decisions,
        "affected_verifications": verifications,
        "invalidated_claims": invalidated,
        "requires_review": bool(changed or metadata_changes),
    }
