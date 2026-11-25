"""The declarative-check interpreter: data in, flagged hunks out, nothing generated is run."""

import time

import pytest

from sre_harness.review import apply_rules

TIMEOUT_CHECK = {
    "path_globs": ["**/*.py"],
    "added_line_pattern": r"client\.get\(",
    "absent_pattern": r"\btimeout\b",
    "message": "This HTTP client has no timeout.",
}


def unit(unit_id, path, hunk):
    added, new = apply_rules.sides(hunk)
    return {"id": unit_id, "path": path, "added": added, "new": new}


MISSING = unit("HU-0001", "src/a.py", "@@ -1,1 +1,2 @@\n x = 1\n+r = client.get(url)")
SET = unit("HU-0002", "src/b.py", "@@ -1,1 +1,2 @@\n x = 1\n+r = client.get(url, timeout=5)")
EXCEPTION = unit(
    "HU-0003",
    "src/c.py",
    "@@ -1,1 +1,3 @@\n x = 1\n+# stream: no timeout on purpose\n+r = client.get(url, stream=True)",
)
REMOVED_ONLY = unit("HU-0004", "src/d.py", "@@ -1,2 +1,1 @@\n x = 1\n-r = client.get(url)")
OTHER_PATH = unit("HU-0005", "docs/a.md", "@@ -1,1 +1,2 @@\n x\n+client.get(url)")


def test_a_check_flags_an_added_line_without_the_absent_pattern_in_its_hunk():
    rules = [{"id": "IC-0001", "check": TIMEOUT_CHECK}]
    results, failures = apply_rules.apply(rules, [MISSING, SET, EXCEPTION, REMOVED_ONLY])
    assert failures == {}
    assert results["IC-0001"]["units"] == ["HU-0001"]


def test_without_absent_pattern_every_matching_added_line_is_flagged():
    check = {key: value for key, value in TIMEOUT_CHECK.items() if key != "absent_pattern"}
    results, _ = apply_rules.apply([{"id": "IC-0001", "check": check}], [MISSING, SET, EXCEPTION])
    assert results["IC-0001"]["units"] == ["HU-0001", "HU-0002", "HU-0003"]


@pytest.mark.parametrize(
    "globs, flagged",
    [
        (["**/*.py"], ["HU-0001"]),
        (["src/*.py"], ["HU-0001"]),
        (["*.py"], []),  # `*` does not cross a directory
        (["docs/**"], ["HU-0005"]),
        (["**"], ["HU-0001", "HU-0005"]),
        (["src/?.py"], ["HU-0001"]),
    ],
)
def test_path_globs_select_the_hunks(globs, flagged):
    check = {**TIMEOUT_CHECK, "path_globs": globs}
    results, _ = apply_rules.apply([{"id": "IC-0001", "check": check}], [MISSING, OTHER_PATH])
    assert results["IC-0001"]["units"] == flagged


@pytest.mark.parametrize(
    "change, reason",
    [
        ({"added_line_pattern": "a" * 201}, "added_line_pattern must be 1-200"),
        ({"absent_pattern": "b" * 201}, "absent_pattern must be 1-200"),
        ({"added_line_pattern": "(unclosed"}, "not a valid regular expression"),
        ({"path_globs": ["../secret/*"]}, "not a relative glob"),
        ({"path_globs": ["/etc/*"]}, "not a relative glob"),
        ({"path_globs": []}, "path_globs is empty"),
    ],
)
def test_a_check_that_cannot_be_interpreted_fails_alone(change, reason):
    rules = [
        {"id": "IC-0001", "check": {**TIMEOUT_CHECK, **change}},
        {"id": "IC-0002", "check": TIMEOUT_CHECK},
    ]
    results, failures = apply_rules.apply(rules, [MISSING])
    assert reason in failures["IC-0001"] and "IC-0001" not in results
    assert results["IC-0002"]["units"] == ["HU-0001"]


def test_a_runaway_pattern_is_stopped_and_the_other_checks_go_on():
    runaway = {**TIMEOUT_CHECK, "added_line_pattern": r"^(a+)+$", "path_globs": ["**"]}
    slow = unit("HU-0009", "src/z.py", "@@ -1,0 +1,1 @@\n+" + "a" * 40 + "!")
    rules = [{"id": "IC-0001", "check": runaway}, {"id": "IC-0002", "check": TIMEOUT_CHECK}]
    started = time.monotonic()
    results, failures = apply_rules.apply(rules, [slow, MISSING], timeout=0.5)
    assert time.monotonic() - started < 10
    assert failures == {"IC-0001": "regex_timeout"}
    assert results["IC-0002"]["units"] == ["HU-0001"]


def test_overlong_lines_are_skipped_and_counted_not_cut():
    long_line = unit("HU-0010", "src/l.py", "@@ -1,0 +1,1 @@\n+client.get(" + "x" * 5000 + ")")
    results, _ = apply_rules.apply([{"id": "IC-0001", "check": TIMEOUT_CHECK}], [long_line])
    assert results["IC-0001"] == {"units": [], "skipped_long_lines": 1}


def test_hunks_and_sides_read_a_unified_diff_patch():
    patch = "@@ -1,2 +1,2 @@ def f():\n a\n-b\n+c\n@@ -9 +9,2 @@\n z\n+y"
    first, second = apply_rules.hunks(patch)
    assert first.startswith("@@ -1,2 +1,2 @@") and second.startswith("@@ -9 +9,2 @@")
    assert apply_rules.sides(first) == (["c"], ["a", "c"])
    assert apply_rules.hunks("no hunk here") == []
