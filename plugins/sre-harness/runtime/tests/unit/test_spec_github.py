"""The GitHub adapter of `spec ingest` against hand-made responses: pages, gaps, odd shapes."""

import json

from sre_harness.github.rest import Limiter, Response
from sre_harness.spec import ingest_github
from tests.helpers import FakeClock

ENTRY = {"repo": "example-org/demo", "pull": 7}
NOW = "2026-01-16T00:00:00Z"
PULL = {"title": "t", "body": "## A\n\ntext", "head": {"sha": "a" * 40}}


class Transport:
    def __init__(self, answers):
        self.answers = answers
        self.asked = []

    def rest(self, endpoint, values, query):
        self.asked.append((endpoint, query.get("page")))
        return self.answers[(endpoint, query.get("page"))]


def ok(value, link=None):
    headers = {"link": link} if link else {}
    return Response(200, headers, json.dumps(value))


def read(answers):
    clock = FakeClock()
    transport = Transport(answers)
    documents, gaps = ingest_github.read_pull(transport, Limiter(clock, clock.sleep, 0), ENTRY, NOW)
    return transport, documents, gaps


def comment(number, body="b", login="alice"):
    return {"id": number, "user": {"login": login}, "body": body}


def test_comment_pages_are_followed_by_their_next_link():
    link = '<https://api.github.com/repos/example-org/demo/issues/7/comments?page=2>; rel="next"'
    transport, (pr, comments), gaps = read(
        {
            ("pull", None): ok(PULL),
            ("issue_comments", 1): ok([comment(1)], link),
            ("issue_comments", 2): ok([comment(2, login=None), {"id": "x"}, comment(3, body=None)]),
        }
    )
    assert gaps == []
    assert [s["locator"] for s in comments["sections"]] == [
        "issue_comment 1",
        "issue_comment 2",
        "issue_comment 3",
    ]
    assert comments["sections"][1]["heading"] == "ghost"
    assert comments["missing"] == ["comments[2]", "comments[3].body"]
    assert transport.asked == [("pull", None), ("issue_comments", 1), ("issue_comments", 2)]


def test_a_comment_page_that_fails_is_input_missing_and_the_body_is_kept():
    _, documents, gaps = read(
        {("pull", None): ok(PULL), ("issue_comments", 1): Response(500, {}, "{}")}
    )
    assert [d["kind"] for d in documents] == ["github_pr", "github_issue_comment"]
    assert gaps[0]["reason"] == "http_500"
    assert gaps[0]["locator"].endswith("comments?page=1")


def test_a_comment_page_of_another_shape_is_input_missing():
    _, _, gaps = read({("pull", None): ok(PULL), ("issue_comments", 1): ok({"not": "a list"})})
    assert gaps[0]["reason"] == "unexpected_shape"


def test_a_pull_that_is_not_an_object_or_not_json_is_input_missing():
    for answer, reason in (
        (ok([1]), "unexpected_shape"),
        (Response(200, {}, "<html>"), "body_not_json"),
    ):
        _, documents, gaps = read({("pull", None): answer})
        assert documents == [] and gaps[0]["reason"] == reason


def test_a_pull_without_title_or_head_lists_them_as_missing():
    _, (pr, _), _ = read({("pull", None): ok({"body": "x"}), ("issue_comments", 1): ok([])})
    assert pr["missing"] == ["title", "head.sha"]
    assert "sha" not in pr["provenance"]
