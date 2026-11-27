"""The interpreter for declarative checks. A check is data; no generated code is ever run.

    check = {path_globs[], added_line_pattern, absent_pattern?, message}

A check flags a hunk when its path matches one of `path_globs`, one of its added lines matches
`added_line_pattern`, and (when `absent_pattern` is given) no line of the hunk's new side
(added and context lines) matches `absent_pattern`.

Patterns come from a backend, so they are capped in length and matched by `guard.regex_search`
in a separate process with a time limit. A check whose match runs out of time, or whose
pattern is invalid, fails alone: it is reported and the other checks go on.
"""

import re

from .. import guard
from ..errors import InputError, QualityGateError
from ..globs import path_matches

MAX_PATTERN_LENGTH = guard.REGEX_MAX_PATTERN_LENGTH
MAX_LINE_LENGTH = 4000
DEFAULT_TIMEOUT = 5.0
_GLOB = re.compile(r"[A-Za-z0-9._/*?-]{1,200}")
HUNK_HEADER = re.compile(r"@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@")


def check_problems(check):
    """Why a check cannot be interpreted; empty when it can."""
    problems = []
    for glob in check.get("path_globs") or []:
        parts = glob.split("/")
        if not _GLOB.fullmatch(glob) or glob.startswith("/") or ".." in parts:
            problems.append(f"path glob {glob!r} is not a relative glob")
    if not check.get("path_globs"):
        problems.append("path_globs is empty")
    for key in ("added_line_pattern", "absent_pattern"):
        pattern = check.get(key)
        if pattern is None and key == "absent_pattern":
            continue
        if not isinstance(pattern, str) or not pattern or len(pattern) > MAX_PATTERN_LENGTH:
            problems.append(f"{key} must be 1-{MAX_PATTERN_LENGTH} characters")
            continue
        try:
            re.compile(pattern)  # syntax only; matching happens in the worker
        except re.error as error:
            problems.append(f"{key} is not a valid regular expression ({error.msg})")
    return problems


def hunks(patch):
    """The hunks of a unified diff patch (GitHub's `patch`), each starting at its header."""
    found, current = [], None
    for line in (patch or "").split("\n"):
        if HUNK_HEADER.match(line):
            current = [line]
            found.append(current)
        elif current is not None:
            current.append(line)
    return ["\n".join(lines) for lines in found]


def sides(hunk):
    """(added lines, new-side lines) of one hunk, without their markers."""
    added, new = [], []
    for line in hunk.split("\n")[1:]:
        if line.startswith("+"):
            added.append(line[1:])
            new.append(line[1:])
        elif line.startswith(" "):
            new.append(line[1:])
        elif line == "":
            new.append("")
    return added, new


def apply(rules, units, timeout=DEFAULT_TIMEOUT, env=None):
    """Interpret each rule's check over the units.

    rules: [{"id", "check"}]. units: [{"id", "path", "added", "new"}].
    Returns {rule id: {"units": [unit ids flagged], "skipped_long_lines": n}} for rules that ran
    and {rule id: reason} for rules that failed.
    """
    results, failures = {}, {}
    for rule in rules:
        check = rule["check"]
        problems = check_problems(check)
        if problems:
            failures[rule["id"]] = "invalid_check: " + "; ".join(problems)
            continue
        selected = [unit for unit in units if path_matches(unit["path"], check["path_globs"])]
        texts, owners, skipped = [], [], 0
        for unit in selected:
            for line in unit["added"]:
                if len(line) > MAX_LINE_LENGTH:
                    skipped += 1
                    continue
                texts.append(line)
                owners.append(unit["id"])
        patterns = [check["added_line_pattern"]]
        absent = check.get("absent_pattern")
        hunk_start = len(texts)
        if absent:
            patterns.append(absent)
            texts += ["\n".join(unit["new"]) for unit in selected]
        flagged = []
        if owners:
            try:
                matches = guard.regex_search(patterns, texts, timeout=timeout, env=env)
            except guard.RegexTimeout:
                failures[rule["id"]] = "regex_timeout"
                continue
            except (InputError, QualityGateError) as error:
                failures[rule["id"]] = f"regex_failed: {error}"
                continue
            hit = {owners[index] for index in matches[0] if index < hunk_start}
            spared = set()
            if absent:
                spared = {
                    selected[index - hunk_start]["id"]
                    for index in matches[1]
                    if index >= hunk_start
                }
            flagged = [unit["id"] for unit in selected if unit["id"] in hit - spared]
        results[rule["id"]] = {"units": flagged, "skipped_long_lines": skipped}
    return results, failures
