"""Counts and metrics on tables small enough to check by hand."""

import pytest

from sre_harness.review import metrics

# Two rules (K1, K2) over five hunks. Labels per cell:
#
#   hunk  K1                  K2
#   u1    human_flagged  F    clean
#   u2    human_flagged       clean          F
#   u3    clean          F    human_missed_valid F
#   u4    clean          F*   disputed       F
#   u5    ambiguous      F    clean
#
# F = a finding, * = adjudicated invalid.
CELLS = {
    ("u1", "K1"): "human_flagged",
    ("u1", "K2"): "clean",
    ("u2", "K1"): "human_flagged",
    ("u2", "K2"): "clean",
    ("u3", "K1"): "clean",
    ("u3", "K2"): "human_missed_valid",
    ("u4", "K1"): "clean",
    ("u4", "K2"): "disputed",
    ("u5", "K1"): "ambiguous",
    ("u5", "K2"): "clean",
}
FLAGGED = {("u1", "K1"), ("u2", "K2"), ("u3", "K1"), ("u3", "K2"), ("u4", "K1"), ("u4", "K2")}
FLAGGED |= {("u5", "K1")}
INVALID = {("u4", "K1")}


def test_counts_leave_disputed_and_ambiguous_cells_out():
    counts = metrics.count(CELLS, FLAGGED, INVALID)
    assert counts == {
        "true_positives": 2,  # u1/K1, u3/K2
        "findings": 5,  # the two above + u2/K2, u3/K1, u4/K1 (u4/K2 and u5/K1 are not counted)
        "findings_on_clean": 3,
        "confirmed_false_positives": 1,  # u4/K1
        "unconfirmed_findings": 2,  # u2/K2, u3/K1
        "positives": 3,  # u1/K1, u2/K1, u3/K2
        "clean_cells": 5,  # u1/K2, u2/K2, u3/K1, u4/K1, u5/K2
    }


def test_metrics_by_hand():
    found = metrics.values(metrics.count(CELLS, FLAGGED, INVALID))
    assert found["precision_lower"] == pytest.approx(2 / 5)
    assert found["precision_confirmed"] == pytest.approx(2 / 3)
    assert found["recall"] == pytest.approx(2 / 3)
    assert found["f1"] == pytest.approx(2 * (2 / 5) * (2 / 3) / ((2 / 5) + (2 / 3)))
    assert found["false_positive_rate"] == pytest.approx(3 / 5)


def test_a_zero_denominator_is_undefined_not_zero():
    found = metrics.values(metrics.count({("u1", "K1"): "clean"}, set()))
    assert found["precision_lower"] is None and found["precision_confirmed"] is None
    assert found["recall"] is None and found["f1"] is None
    assert found["false_positive_rate"] == 0.0


def per_pr(n):
    """n PRs: every third PR has a missed positive, every fifth a finding on a clean hunk."""
    rows = []
    for number in range(n):
        cells = {(f"a{number}", "K"): "human_flagged", (f"b{number}", "K"): "clean"}
        flagged = set()
        if number % 3:
            flagged.add((f"a{number}", "K"))
        if number % 5 == 0:
            flagged.add((f"b{number}", "K"))
        rows.append(metrics.count(cells, flagged))
    return rows


def test_the_bootstrap_resamples_prs_and_is_reproducible_with_its_seed():
    rows = per_pr(40)
    first = metrics.bootstrap(rows, seed=7, resamples=300, confidence=0.95)
    again = metrics.bootstrap(rows, seed=7, resamples=300, confidence=0.95)
    other = metrics.bootstrap(rows, seed=8, resamples=300, confidence=0.95)
    assert first == again
    assert first != other
    low, high = first["recall"]
    point = metrics.values(metrics.add(rows))["recall"]
    assert low <= point <= high and low < high


def test_fewer_than_thirty_prs_gives_no_number():
    summary = metrics.summarize(per_pr(29), 29)
    assert summary["counts"]["true_positives"] > 0
    for metric in summary["metrics"].values():
        assert metric["value"] is None and metric["ci95"] is None and not metric["defined"]
        assert metric["reason"].startswith("unevaluable: 29 evaluation PRs")


def test_thirty_prs_give_numbers_with_intervals():
    summary = metrics.summarize(per_pr(30), 30)
    recall = summary["metrics"]["recall"]
    assert recall["defined"] and recall["value"] == pytest.approx(20 / 30)
    assert recall["ci95"][0] <= recall["value"] <= recall["ci95"][1]


@pytest.mark.parametrize(
    "change",
    [
        {"split_sha256": "b" * 64},
        {"metrics": {**metrics.METRICS, "recall": "something else"}},
        {"thresholds": {**metrics.THRESHOLDS, "min_prs": 20}},
        {"thresholds": {**metrics.THRESHOLDS, "seed": 1}},
        {"label_rules": {**metrics.LABEL_RULES, "clean": "changed"}},
    ],
)
def test_the_criteria_hash_changes_with_split_metrics_and_thresholds(change):
    base = {"split_sha256": "a" * 64}
    assert metrics.criteria_hash(**base) == metrics.criteria_hash(
        "a" * 64, metrics.METRICS, metrics.THRESHOLDS, metrics.LABEL_RULES
    )
    assert metrics.criteria_hash(**{**base, **change}) != metrics.criteria_hash(**base)
