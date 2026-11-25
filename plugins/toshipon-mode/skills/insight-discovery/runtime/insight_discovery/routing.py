"""Who a hypothesis belongs to after the audit: research here, or one of the two neighbours.

Autonomous Improvement owns artifacts and Harness Evolution owns working methods. Insight
Discovery keeps only questions that neither of them already answers. A handoff is a note for
a person; nothing is registered or adopted on the other side from here.
"""

from . import state as st
from .foundation import write_json

HANDOFFS = {"handoff_evolution": "evolution", "handoff_improvement": "improvement"}


def route(audit, independence):
    """(disposition, reason). The audit can only move a hypothesis out, never back in."""
    if independence not in st.INDEPENDENT:
        return "skip", "audit_not_independent"
    recommendation = audit["recommendation"]
    if recommendation == "reject":
        return "skip", "rejected_by_audit"
    if recommendation in HANDOFFS:
        return recommendation, audit["existing_sufficient"]["basis"]
    if audit["falsifiable"]["verdict"] == "no":
        return "skip", "not_falsifiable"
    if audit["problem_framing"]["verdict"] == "misframed":
        return "skip", "misframed"
    if audit["existing_sufficient"]["verdict"] == "yes":
        return "skip", "existing_method_sufficient"
    return "research", "監査で、既存手法では足りず、反証可能と判断された"


def handoff(home, candidate, target, reason, now):
    """Write the note and record it. The note carries no criteria and changes none."""
    reference = f"handoffs/{candidate['id']}.json"
    note = {
        "insight_id": candidate["id"],
        "target": target,
        "reason": reason,
        "abstracted_problem": candidate["abstracted_problem"],
        "statement": candidate["hypothesis"]["statement"],
        "falsification": candidate["refutation"]["falsification"],
        "evidence_refs": candidate["evidence_refs"],
        "handoff_ref": reference,
    }
    write_json(st.root(home) / reference, note)
    st.append(home, "handoff_recorded", note, now)
    return note
