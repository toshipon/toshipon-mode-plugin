"""jsonschema matches `pattern` with `re.search`, where `$` also matches before a newline at the
end. A name, an id or a one-line string of the knowledge schemas must not take one: their
patterns end with `(?![\\s\\S])` (the end of the string in Python and in ECMA alike)."""

import json

import pytest

from sre_harness import records

SCHEMAS = (
    "knowledge-check-result",
    "knowledge-config",
    "knowledge-fetch-result",
    "knowledge-record",
    "knowledge-search-result",
    "task-context",
    "decision-request",
    "decision-response",
    "source-adapter",
)
END = "(?![\\s\\S])"


def patterns(value):
    if isinstance(value, dict):
        for key, inner in value.items():
            if key == "pattern" and isinstance(inner, str):
                yield inner
            else:
                yield from patterns(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from patterns(inner)


@pytest.mark.parametrize("name", SCHEMAS)
def test_no_pattern_ends_with_a_dollar_that_takes_a_trailing_newline(name):
    found = list(patterns(records.schema(name)))
    assert [pattern for pattern in found if pattern.endswith("$")] == []


@pytest.mark.parametrize(
    ("name", "definition", "good"),
    [
        ("knowledge-record", "record_id", "KN-000000000000"),
        ("knowledge-record", "name", "project-alpha"),
        ("knowledge-record", "summary", "one line"),
        ("knowledge-record", "labels", ["retention"]),
        ("decision-request", "request_id", "DR-000000000000"),
        ("decision-request", "question", "保持期間を延ばしてよいか"),
    ],
)
def test_a_value_with_a_newline_at_the_end_is_refused(name, definition, good):
    assert records.violations(name, good, definition) == []
    bad = [good[0] + "\n"] if isinstance(good, list) else good + "\n"
    assert records.violations(name, bad, definition) != []


def test_a_task_file_with_a_newline_at_the_end_of_a_value_is_refused():
    task = {
        "schema_version": 1,
        "task_id": "TASK-0001",
        "workspace_id": "ws-a",
        "principal": "workspace-a",
        "project_id": "project-alpha",
        "component_id": None,
        "repo": "example-org/alpha",
        "base_sha": "a" * 40,
        "changed_paths": ["infra/main.tf"],
        "resource_types": [],
    }
    assert records.violations("task-context", task) == []
    for field, value in [
        ("task_id", "TASK-0001\n"),
        ("repo", "example-org/alpha\n"),
        ("base_sha", "a" * 40 + "\n"),
        ("changed_paths", ["infra/main.tf\n"]),
    ]:
        changed = json.loads(json.dumps(task))
        changed[field] = value
        assert records.violations("task-context", changed) != [], field
