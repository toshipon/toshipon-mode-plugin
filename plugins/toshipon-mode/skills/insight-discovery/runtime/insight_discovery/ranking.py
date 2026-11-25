"""Order candidates from ordinal scores. Deterministic: same scores, same order."""

MAX_PRESENTED = 3
AXES = ("value", "evidence", "delta_from_prior_art", "testability", "cost")
EVIDENCE_SCORE = {"candidate": 1, "recurring": 2, "strong": 3}


def evidence_score(summary):
    """The evidence axis is computed from the evidence, not asked of a model."""
    return {
        "score": EVIDENCE_SCORE[summary["strength"]],
        "reason": (
            f"証拠 {summary['count']} 件、業務 {summary['distinct_contexts']} 種、"
            f"反復の強さ {summary['strength']}"
        ),
    }


def total(candidate):
    return sum(candidate["scores"][axis]["score"] for axis in AXES)


def rank(candidates):
    """Best first. Ties go to stronger evidence, then more tasks, then a fixed order."""
    return sorted(
        candidates,
        key=lambda c: (
            -total(c),
            -c["scores"]["evidence"]["score"],
            -c["evidence_summary"]["distinct_contexts"],
            c["fingerprint"],
        ),
    )


def presentable(state, settings):
    """Open research candidates, best first, never more than three."""
    open_research = [
        candidate
        for candidate in state["candidates"].values()
        if candidate["status"] == "candidate"
        and candidate["routing"]["disposition"] == "research"
        and candidate["novelty"]["status"] != "covered_by_existing"
    ]
    return rank(open_research)[: min(settings["gates"]["max_candidates"], MAX_PRESENTED)]
