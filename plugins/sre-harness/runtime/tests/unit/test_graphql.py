import json

import pytest

from examples.synthetic.github_responses import GitHubFixture
from sre_harness import guard
from sre_harness.github import graphql, rest
from sre_harness.github.fixture import FixtureTransport
from tests.helpers import FakeClock


class Sink:
    def __init__(self):
        self.saved, self.missed, self.rates = [], [], []

    def save(self, request, response, body):
        self.saved.append((request, body))

    def miss(self, request, response, reason, **extra):
        self.missed.append((request, reason, extra))

    def graphql_rate(self, rate):
        self.rates.append(rate)


def walk(transport, clock=None, number=1):
    clock = clock or FakeClock()
    sink = Sink()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=3600)
    graphql.walk_threads(transport, limiter, sink, "example-org", "demo", number)
    return sink, clock


def test_bundled_queries_read_what_normalize_needs():
    threads = guard.graphql_query(graphql.THREADS)
    comments = guard.graphql_query(graphql.COMMENTS)
    for field in ("isResolved", "isOutdated", "resolvedBy", "path", "line", "originalLine"):
        assert field in threads
    for text in (threads, comments):
        for field in ("originalCommit", "commit", "replyTo", "authorAssociation", "__typename"):
            assert field in text
        assert "rateLimit { cost remaining resetAt }" in " ".join(text.split())


def test_threads_and_comments_are_walked_with_two_cursors(tmp_path):
    fixture = GitHubFixture(thread_page_size=1, comment_page_size=2)
    pr = fixture.pull(1)
    for index in range(3):
        thread = pr.thread(path=f"src/f{index}.py")
        for _ in range(5 if index == 1 else 1):
            thread.comment("bob", "Synthetic.")
    transport = FixtureTransport(fixture.write(tmp_path / "fx"))
    sink, _ = walk(transport)
    assert sink.missed == []
    queries = [request["query"] for request, _ in sink.saved]
    # three thread pages; the 5-comment thread needs two more comment pages (2 + 2 + 1)
    assert queries == [
        "review_threads",
        "review_threads",
        "thread_comments",
        "thread_comments",
        "review_threads",
    ]
    assert all("graphql:" in key for key in transport.requests)
    assert len(sink.rates) == 5


def body(data=None, errors=None):
    content = {}
    if data is not None:
        content["data"] = data
    if errors is not None:
        content["errors"] = errors
    return json.dumps(content)


class Scripted:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def graphql(self, name, variables):
        self.calls.append((name, variables))
        return self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]


EMPTY = {
    "repository": {
        "pullRequest": {
            "reviewThreads": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}
        }
    }
}


def test_an_exhausted_graphql_quota_holds_until_reset_at():
    clock = FakeClock()
    data = {**EMPTY, "rateLimit": {"cost": 1, "remaining": 0, "resetAt": "2026-01-01T00:10:00Z"}}
    transport = Scripted(rest.Response(200, {}, body(data)))
    sink, _ = walk(transport, clock)
    assert sink.rates == [{"cost": 1, "remaining": 0, "resetAt": "2026-01-01T00:10:00Z"}]
    limiter = rest.Limiter(clock, clock.sleep, max_wait=3600)
    graphql.query(transport, limiter, sink, graphql.THREADS, {}, {"kind": "graphql"})
    limiter.before({"kind": "graphql"})
    assert clock.slept == [600]


def test_a_rate_limited_error_is_waited_out_and_retried():
    limited = rest.Response(
        200,
        {"x-ratelimit-reset": "1767225660"},
        body(errors=[{"type": "RATE_LIMITED", "message": "slow down"}]),
    )
    transport = Scripted(limited, rest.Response(200, {}, body(EMPTY)))
    sink, clock = walk(transport)
    assert clock.slept == [60]
    assert sink.missed == [] and len(transport.calls) == 2


@pytest.mark.parametrize(
    "response, reason",
    [
        (rest.Response(502, {}, "bad gateway"), "http_502"),
        (rest.Response(200, {}, "<html>"), "body_not_json"),
        (rest.Response(200, {}, "[]"), "unexpected_shape"),
        (rest.Response(None), "no_http_response"),
    ],
)
def test_an_unusable_answer_is_recorded_as_missing(response, reason):
    sink, _ = walk(Scripted(response))
    assert [missed[1] for missed in sink.missed] == [reason]
    assert sink.saved == []


def test_errors_beside_data_are_recorded_and_the_data_kept():
    errors = [{"type": "FORBIDDEN", "path": ["x"], "message": "Resource not accessible by token"}]
    sink, _ = walk(Scripted(rest.Response(200, {}, body(EMPTY, errors))))
    assert sink.missed[0][1:] == ("graphql_errors", {"types": ["FORBIDDEN"]})
    assert len(sink.saved) == 1


def test_a_missing_pull_request_is_recorded():
    data = {"repository": {"pullRequest": None}}
    sink, _ = walk(Scripted(rest.Response(200, {}, body(data, [{"type": "NOT_FOUND"}]))))
    assert [missed[1] for missed in sink.missed] == ["graphql_errors", "no_review_threads"]


def test_a_repeated_cursor_does_not_loop():
    page = {
        "repository": {
            "pullRequest": {
                "reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "Y3Vyc29yOjE="},
                    "nodes": [],
                }
            }
        }
    }
    transport = Scripted(rest.Response(200, {}, body(page)))
    sink, _ = walk(transport)
    assert len(transport.calls) == 2
    assert [missed[1] for missed in sink.missed] == ["unusable_cursor"]
