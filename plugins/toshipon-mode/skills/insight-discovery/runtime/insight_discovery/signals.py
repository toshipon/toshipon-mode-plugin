"""Deterministic signals: what repeats, where, and how strongly. No model is involved."""

from collections import defaultdict

STRENGTHS = ("candidate", "recurring", "strong")


def strength(occurrences, contexts):
    """candidate: seen once or twice in one task. strong: many times across three or more tasks."""
    if contexts >= 3 and occurrences >= 5:
        return "strong"
    if contexts >= 2 or occurrences >= 3:
        return "recurring"
    return "candidate"


def summarize(items):
    """count, distinct tasks and strength of a set of evidence."""
    occurrences = sum(item["counts"]["occurrences"] for item in items)
    contexts = len({item["context"]["task_key"] for item in items})
    return {
        "count": len(items),
        "distinct_contexts": contexts,
        "strength": strength(occurrences, contexts),
    }


def _category(kind, occurrences, contexts):
    if kind == "unexpected_success":
        return "unexpected_success"
    if occurrences == 1:
        return "exception"
    if contexts >= 2 or kind in ("retry", "failure"):
        return "repetition"
    return "friction"


def extract(items):
    """One signal per (kind, label), strongest first."""
    groups = defaultdict(list)
    for item in items:
        groups[(item["kind"], item["label"])].append(item)
    signals = []
    for (kind, label), members in groups.items():
        occurrences = sum(item["counts"]["occurrences"] for item in members)
        contexts = sorted({item["context"]["task_key"] for item in members})
        signals.append(
            {
                "kind": kind,
                "label": label,
                "category": _category(kind, occurrences, len(contexts)),
                "occurrences": occurrences,
                "sessions": sum(item["counts"]["sessions"] for item in members),
                "contexts": contexts,
                "strength": strength(occurrences, len(contexts)),
                "evidence_ids": sorted(item["id"] for item in members),
            }
        )
    order = {name: index for index, name in enumerate(reversed(STRENGTHS))}
    return sorted(
        signals, key=lambda s: (order[s["strength"]], -s["occurrences"], s["kind"], s["label"])
    )
