"""Deterministic checks on what a model returned. A model never decides what passes.

Each check returns (accepted, rejected). Accepted items get the ids later stages refer to
(O1, G1, H1). Rejected items keep a reason, and show up in the report as "not presented".
"""

import difflib
import re

from . import signals

# Assertions of novelty. Whether something is new is a status computed from recorded searches
# (novelty.status), so a sentence that claims it is never accepted from a model.
NOVELTY_CLAIM = re.compile(
    r"新規性|前例(?:が|は|の|も)?\s*(?:な[いく]|無[いく])|世界初|史上初|業界初|画期的|未踏"
    r"|誰も\S{0,12}(?:ていない|てない|たことがない)"
    r"|\bnovel(?:ty)?\b|\bunprecedented\b|\bfirst[- ]ever\b|\bgroundbreaking\b"
    r"|\bno (?:prior|existing|previous) (?:work|art|method|approach)"
    r"|\bnever been (?:done|tried|studied|explored)\b",
    re.IGNORECASE,
)


def claims_novelty(*texts):
    return any(NOVELTY_CLAIM.search(text) for text in texts if isinstance(text, str))


def _blank(*texts):
    return any(not str(text).strip() for text in texts)


def _strings(value):
    """Every string inside a JSON value."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from _strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from _strings(child)


def _reject(rejected, ref, reason, **extra):
    rejected.append({"ref": str(ref), "reason": reason, **extra})


def similar(first, second):
    return difflib.SequenceMatcher(None, first, second).ratio()


def observe(output, evidence):
    """Keep observations whose every reference exists and whose every quote is verbatim."""
    accepted, rejected, position_to_id = [], [], {}
    for position, item in enumerate(output["observations"]):
        refs = list(dict.fromkeys(item["evidence_refs"]))
        sources = [
            evidence[ref].get("text") or evidence[ref]["label"] for ref in refs if ref in evidence
        ]
        if not refs:
            _reject(rejected, position, "no_evidence")
        elif len(sources) != len(refs):
            _reject(rejected, position, "unknown_evidence")
        elif _blank(item["text"]):
            _reject(rejected, position, "blank")
        elif claims_novelty(item["text"]):
            _reject(rejected, position, "unsupported_novelty_claim")
        elif any(not any(quote in source for source in sources) for quote in item["quotes"]):
            _reject(rejected, position, "quote_not_in_source")
        else:
            position_to_id[position] = f"O{len(accepted) + 1}"
            accepted.append(
                {
                    "id": position_to_id[position],
                    "text": item["text"],
                    "evidence_refs": refs,
                    "quotes": item["quotes"],
                }
            )
    interpretations = []
    for position, item in enumerate(output["interpretations"]):
        based_on = item["based_on"]
        if (
            based_on
            and all(index in position_to_id for index in based_on)
            and not _blank(item["text"])
        ):
            interpretations.append(
                {"text": item["text"], "based_on": [position_to_id[index] for index in based_on]}
            )
        else:
            _reject(rejected, f"interpretation {position}", "interpretation_without_observation")
    return {"observations": accepted, "interpretations": interpretations}, rejected


def structure(output, observations, evidence, gates):
    """Keep groups that rest on enough evidence from enough different tasks."""
    known = {item["id"]: item for item in observations}
    accepted, rejected = [], []
    for position, group in enumerate(output["groups"]):
        members = list(dict.fromkeys(group["observations"]))
        if not members:
            _reject(rejected, position, "no_observation")
            continue
        if any(member not in known for member in members):
            _reject(rejected, position, "unknown_observation")
            continue
        refs = sorted({ref for member in members for ref in known[member]["evidence_refs"]})
        summary = signals.summarize([evidence[ref] for ref in refs])
        texts = [group["abstracted_problem"], group["doubted_premise"], *group["levels"].values()]
        if _blank(*texts):
            _reject(rejected, position, "blank")
        elif claims_novelty(*texts):
            _reject(rejected, position, "unsupported_novelty_claim")
        elif summary["count"] < gates["min_evidence"]:
            _reject(rejected, position, "insufficient_evidence", evidence=summary["count"])
        elif summary["distinct_contexts"] < gates["min_distinct_contexts"]:
            # One task repeating itself is a local problem, not a structure shared across tasks.
            _reject(rejected, position, "single_context", contexts=summary["distinct_contexts"])
        else:
            accepted.append(
                {
                    "id": f"G{len(accepted) + 1}",
                    "observations": members,
                    "evidence_refs": refs,
                    "evidence_summary": summary,
                    "abstracted_problem": group["abstracted_problem"],
                    "levels": group["levels"],
                    "doubted_premise": group["doubted_premise"],
                }
            )
    return {"groups": accepted}, rejected


def _duplicate(statement, group, state, threshold):
    """(reason, candidate id) when the statement repeats a known candidate, else None."""
    for candidate in state["candidates"].values():
        if similar(statement, candidate["hypothesis"]["statement"]) < threshold:
            continue
        if candidate["status"] != "rejected":
            return "duplicate_of_existing", candidate["id"]
        rejected_at = state["resolved_seq"].get(candidate["id"], 0)
        if not any(state["evidence_seq"][ref] > rejected_at for ref in group["evidence_refs"]):
            return "duplicate_of_rejected", candidate["id"]
    return None


def hypothesize(output, groups, state, gates):
    """Keep refutable, non-repeating hypotheses, from groups looked at in more than one way."""
    known = {group["id"]: group for group in groups}
    survivors, rejected = [], []
    for position, item in enumerate(output["hypotheses"]):
        group = known.get(item["group"])
        analogy = item.get("analogy")
        if not group:
            _reject(rejected, position, "unknown_group")
        elif _blank(item["statement"], item["falsification"]):
            _reject(rejected, position, "no_falsification")
        elif item["generator"] == "analogy" and (not analogy or _blank(*analogy.values())):
            _reject(rejected, position, "analogy_missing")
        elif claims_novelty(*_strings(item)):
            _reject(rejected, position, "unsupported_novelty_claim")
        elif duplicate := _duplicate(item["statement"], group, state, gates["similarity"]):
            _reject(rejected, position, duplicate[0], candidate=duplicate[1])
        elif any(
            similar(item["statement"], other["statement"]) >= gates["similarity"]
            for _, other in survivors
        ):
            _reject(rejected, position, "duplicate_in_batch")
        else:
            survivors.append((position, item))
    accepted = []
    for position, item in survivors:
        generators = {
            other["generator"] for _, other in survivors if other["group"] == item["group"]
        }
        if len(generators) < 2:
            _reject(rejected, position, "single_generator")
            continue
        accepted.append(
            {
                "id": f"H{len(accepted) + 1}",
                "group": item["group"],
                "statement": item["statement"],
                "generator": item["generator"],
                "analogy": item.get("analogy"),
                "falsification": item["falsification"],
                "counter_evidence": item["counter_evidence"],
            }
        )
    return {"hypotheses": accepted}, rejected


def audit(output, hypotheses):
    """One audit per hypothesis. A finding without evidence is dropped; a hypothesis without an audit fails."""
    known = {item["id"] for item in hypotheses}
    accepted, rejected, seen = [], [], set()
    for item in output["audits"]:
        ref = item["hypothesis"]
        bases = [
            item[key]["basis"] for key in ("existing_sufficient", "problem_framing", "falsifiable")
        ]
        if ref not in known or ref in seen:
            _reject(rejected, ref, "unknown_hypothesis")
        elif _blank(*bases):
            _reject(rejected, ref, "audit_without_basis")
        else:
            seen.add(ref)
            findings = [f for f in item["findings"] if not _blank(f["claim"], f["evidence"])]
            for _ in range(len(item["findings"]) - len(findings)):
                _reject(rejected, ref, "finding_without_evidence")
            accepted.append({**item, "findings": findings})
    for ref in sorted(known - seen, key=lambda name: int(name[1:])):
        _reject(rejected, ref, "not_audited")
    return accepted, rejected


def assess(output, research):
    """Keep assessments with a reason for every score and a complete minimal experiment."""
    accepted, rejected, seen = [], [], set()
    for item in output["assessments"]:
        ref = item["hypothesis"]
        experiment = item["minimal_experiment"]
        if ref not in research or ref in seen:
            _reject(rejected, ref, "unknown_hypothesis")
            continue
        seen.add(ref)
        if _blank(*(score["reason"] for score in item["scores"].values())):
            _reject(rejected, ref, "score_without_reason")
        elif (
            _blank(experiment["procedure"], experiment["comparison"])
            or _blank(item["success_criteria"], item["kill_criteria"], item["confidence_reason"])
            or experiment["estimated_hours"] <= 0
        ):
            _reject(rejected, ref, "incomplete_experiment")
        elif claims_novelty(*_strings(item)):
            _reject(rejected, ref, "unsupported_novelty_claim")
        else:
            accepted.append(item)
    for ref in sorted(set(research) - seen, key=lambda name: int(name[1:])):
        _reject(rejected, ref, "not_assessed")
    return {"assessments": accepted}, rejected
