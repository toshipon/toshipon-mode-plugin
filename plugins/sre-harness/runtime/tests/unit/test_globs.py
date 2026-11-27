"""Path globs: `**/` repeated is one `**/`, and a glob whose `**` could make the match slow is
refused where it comes into the knowledge index. Everything is synthetic."""

import time

import pytest

from sre_harness import globs
from tests.helpers import run_cli
from tests.knowledge_helpers import item, update, write_config, write_input

SLOW_PATH = "a/" * 40 + "b"


def test_repeated_double_stars_are_one():
    assert globs.glob_regex("**/" * 12 + "z").pattern == globs.glob_regex("**/z").pattern
    assert globs.glob_regex("infra/**/**/main.tf").pattern == (
        globs.glob_regex("infra/**/main.tf").pattern
    )
    for path, expected in [("z", True), ("a/z", True), ("a/b/z", True), ("a/zz", False)]:
        assert globs.path_matches(path, ["**/**/z"]) is expected, path


def test_many_repeated_double_stars_match_at_once():
    started = time.monotonic()
    assert globs.path_matches(SLOW_PATH, ["**/" * 12 + "z"]) is False
    assert time.monotonic() - started < 5


@pytest.mark.parametrize(
    ("glob", "problem"),
    [
        ("infra/**/main.tf", None),
        ("**/" * 12 + "z", None),
        ("**/a/" * 20 + "z", "invalid_glob"),
        ("x" * 201, "invalid_glob"),
    ],
)
def test_a_glob_with_too_many_double_stars_or_too_long_is_a_problem(glob, problem):
    assert globs.problem(glob) == problem


def test_ingest_rejects_an_invalid_glob_and_search_answers_quickly(home, tmp_path):
    write_config(home)
    collapsed = item("G", facets={"topic": ["retention"], "path_globs": ["**/" * 12 + "z"]})
    slow = item("S", facets={"topic": ["retention"], "path_globs": ["**/a/" * 20 + "z"]})
    code, result = update(home, write_input(tmp_path / "in.json", [collapsed, slow]))
    assert code == 0
    assert [(row["index"], row["reason"]) for row in result["rejected"]] == [(1, "invalid_glob")]
    argv = ["knowledge", "search", "--principal", "workspace-b", "--project", "project-alpha"]
    started = time.monotonic()
    code, found = run_cli([*argv, "--path", SLOW_PATH], home)
    assert code == 0 and found["status"] == "ok"
    assert time.monotonic() - started < 5


def test_single_stars_do_not_backtrack_either():
    # `*a*a*…*b` against a long run of `a` is polynomial for a backtracking matcher; the limits
    # of `problem` do not count single stars, so the matcher itself must be linear.
    import time

    glob = "*a" * 60 + "*b"
    assert globs.problem(glob) is None
    started = time.monotonic()
    assert globs.path_matches("a" * 400, [glob]) is False
    assert time.monotonic() - started < 5


def test_the_matcher_agrees_with_the_regular_expression_reading():
    import itertools
    import random

    rng = random.Random(20261127)
    pieces = ["**/", "**", "*", "?", "a", "b", "/", ".", "ab"]
    alphabet = ["a", "b", "/", "."]
    for _ in range(4000):
        glob = "".join(rng.choice(pieces) for _ in range(rng.randint(0, 6)))
        if globs.collapsed(glob).count("*") > 6:
            continue
        for path in itertools.islice(
            ("".join(rng.choice(alphabet) for _ in range(rng.randint(0, 7))) for _ in iter(int, 1)),
            6,
        ):
            expected = globs.glob_regex(glob).fullmatch(path) is not None
            assert globs.path_matches(path, [glob]) is expected, (glob, path)
