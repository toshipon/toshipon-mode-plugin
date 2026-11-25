"""Labels, counts, metrics and PR-level bootstrap for `review evaluate`. Pure functions.

A cell is one hunk and one kind of finding (one rule). Its label:

- human_flagged: a reviewer raised that kind on the hunk and a fix was found.
- human_missed_valid: nobody raised it, and a person adjudicated the rule's finding as valid.
- clean: neither. A finding here is wrong until a person says otherwise.
- disputed: raised but no fix was found (argued, unanswered or unreachable). Not counted.
- ambiguous: raised, and the fix could not be told apart (`fix.status = ambiguous`). Not counted.

Two precisions show how much the numbers lean on reviewers being right:

- precision_lower counts every finding no person confirmed as wrong.
- precision_confirmed counts only findings a reviewer raised or a person adjudicated.

`criteria_hash` freezes the split, these definitions and the thresholds: changing any of them
makes a different evaluation.
"""

import math
import random

from .. import records

LABELS = ("human_flagged", "human_missed_valid", "clean", "disputed", "ambiguous")
POSITIVE = frozenset({"human_flagged", "human_missed_valid"})
COUNTED = POSITIVE | {"clean"}
COUNTS = (
    "true_positives",
    "findings",
    "findings_on_clean",
    "confirmed_false_positives",
    "unconfirmed_findings",
    "positives",
    "clean_cells",
)
METRICS = {
    "precision_lower": "true_positives / findings; a finding no person confirmed counts as wrong",
    "precision_confirmed": "true_positives / (true_positives + confirmed_false_positives); only "
    "findings a reviewer raised or a person adjudicated",
    "recall": "true_positives / positives (human_flagged + human_missed_valid)",
    "f1": "harmonic mean of precision_lower and recall",
    "false_positive_rate": "findings_on_clean / clean_cells",
}
LABEL_RULES = {
    "unit": "hunk x kind of finding (one rule)",
    "kind_match": "a thread's first comment shares at least min(3, n) and at least half of the n "
    "word tokens of the rule's problem",
    "human_flagged": "a matching thread on the hunk has fix.status found",
    "ambiguous": "a matching thread has fix.status ambiguous (left out)",
    "disputed": "matching threads exist but none has a fix (left out)",
    "human_missed_valid": "no matching thread and the adjudication says valid",
    "clean": "otherwise",
}
THRESHOLDS = {"min_prs": 30, "confidence": 0.95, "resamples": 1000, "seed": 20260127}


def criteria_hash(split_sha256, metrics=None, thresholds=None, label_rules=None):
    return records.fingerprint(
        {
            "split_sha256": split_sha256,
            "metrics": METRICS if metrics is None else metrics,
            "thresholds": THRESHOLDS if thresholds is None else thresholds,
            "labels": LABEL_RULES if label_rules is None else label_rules,
        }
    )


def count(cells, flagged, invalid=frozenset()):
    """Counts over `cells` ({cell: label}). `flagged` and `invalid` are sets of cells."""
    tp = on_clean = confirmed = 0
    positives = clean = 0
    for cell, label in cells.items():
        if label in POSITIVE:
            positives += 1
            tp += cell in flagged
        elif label == "clean":
            clean += 1
            if cell in flagged:
                on_clean += 1
                confirmed += cell in invalid
    return {
        "true_positives": tp,
        "findings": tp + on_clean,
        "findings_on_clean": on_clean,
        "confirmed_false_positives": confirmed,
        "unconfirmed_findings": on_clean - confirmed,
        "positives": positives,
        "clean_cells": clean,
    }


def add(counts):
    total = dict.fromkeys(COUNTS, 0)
    for item in counts:
        for key in COUNTS:
            total[key] += item[key]
    return total


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def values(counts):
    """{metric: value or None}. None means the denominator is zero."""
    tp = counts["true_positives"]
    precision = _ratio(tp, counts["findings"])
    recall = _ratio(tp, counts["positives"])
    if precision is None or recall is None:
        f1 = None
    else:
        f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    return {
        "precision_lower": precision,
        "precision_confirmed": _ratio(tp, tp + counts["confirmed_false_positives"]),
        "recall": recall,
        "f1": f1,
        "false_positive_rate": _ratio(counts["findings_on_clean"], counts["clean_cells"]),
    }


def _quantile(ordered, q):
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def bootstrap(per_pr, seed, resamples, confidence):
    """95% (or `confidence`) percentile intervals from resampling whole PRs with replacement.

    Hunks of one PR are not independent, so a PR is the unit that is drawn. Returns
    {metric: [low, high] or None}; None when no resample had a defined value.
    """
    rng = random.Random(seed)
    drawn = {name: [] for name in METRICS}
    n = len(per_pr)
    for _ in range(resamples if n else 0):
        sample = add(per_pr[rng.randrange(n)] for _ in range(n))
        for name, value in values(sample).items():
            if value is not None:
                drawn[name].append(value)
    tail = (1 - confidence) / 2
    intervals = {}
    for name, found in drawn.items():
        found.sort()
        intervals[name] = (
            [round(_quantile(found, tail), 6), round(_quantile(found, 1 - tail), 6)]
            if found
            else None
        )
    return intervals


def summarize(per_pr, n_prs, thresholds=None):
    """{"counts", "metrics"} for one configuration. Under min_prs PRs no value is given."""
    thresholds = THRESHOLDS if thresholds is None else thresholds
    total = add(per_pr)
    found = values(total)
    metrics = {}
    if n_prs < thresholds["min_prs"]:
        reason = f"unevaluable: {n_prs} evaluation PRs, fewer than {thresholds['min_prs']}"
        for name in METRICS:
            metrics[name] = {"value": None, "ci95": None, "defined": False, "reason": reason}
        return {"counts": total, "metrics": metrics}
    intervals = bootstrap(
        per_pr, thresholds["seed"], thresholds["resamples"], thresholds["confidence"]
    )
    for name, value in found.items():
        if value is None:
            metrics[name] = {
                "value": None,
                "ci95": None,
                "defined": False,
                "reason": "undefined: the denominator is 0",
            }
        else:
            metrics[name] = {"value": round(value, 6), "ci95": intervals[name], "defined": True}
    return {"counts": total, "metrics": metrics}
