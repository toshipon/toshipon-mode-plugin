from types import SimpleNamespace

import pytest

from sre_harness import guard
from sre_harness.github import rest
from tests.helpers import TOKEN, FakeClock


def test_status_headers_and_body_are_split_at_the_first_empty_line():
    text = (
        "HTTP/2.0 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "X-RateLimit-Remaining: 42\r\n"
        "\r\n"
        '[{"body": "line one\\r\\n\\r\\nline two"}]'
    )
    response = rest.parse_response(text)
    assert response.status == 200 and response.ok
    assert response.headers == {"content-type": "application/json", "x-ratelimit-remaining": "42"}
    assert response.data() == [{"body": "line one\r\n\r\nline two"}]


def test_lf_only_and_repeated_headers_are_read():
    text = (
        "HTTP/1.1 404 Not Found\n"
        'Link: <https://api.github.com/x?page=2>; rel="next"\n'
        'Link: <https://api.github.com/x?page=5>; rel="last"\n'
        "not a header line\n"
        "\n"
        '{"message": "Not Found"}'
    )
    response = rest.parse_response(text, exit_code=1)
    assert response.status == 404 and not response.ok and response.exit_code == 1
    assert rest.link_pages(response.headers["link"]) == {"next": 2, "last": 5}


def test_a_body_that_is_not_json_is_reported_not_guessed():
    response = rest.parse_response("HTTP/2.0 200 OK\r\n\r\n<html>maintenance</html>")
    assert response.ok
    with pytest.raises(ValueError):
        response.data()


@pytest.mark.parametrize("text", ["", '[{"number": 1}]', "gh: not found\n", "HTTP/2.0 abc\r\n\r\n"])
def test_output_without_a_status_line_is_no_response(text):
    response = rest.parse_response(text)
    assert response.status is None and not response.ok
    assert rest.failure_reason(response) == "no_http_response"


def test_headers_without_a_body():
    response = rest.parse_response("HTTP/2.0 204 No Content\r\nX-A: 1")
    assert response.status == 204 and response.body == "" and response.headers == {"x-a": "1"}


def test_an_oversized_response_stops_instead_of_being_cut(monkeypatch):
    monkeypatch.setattr(rest, "MAX_RESPONSE_BYTES", 64)
    with pytest.raises(rest.ResponseTooLarge) as stopped:
        rest.parse_response("HTTP/2.0 200 OK\r\n\r\n" + "x" * 100)
    assert stopped.value.exit_code == 1


@pytest.mark.parametrize(
    "link, expected",
    [
        ('<https://api.github.com/repos/o/r/pulls?per_page=100&page=3>; rel="next"', {"next": 3}),
        ('<https://evil.invalid/other/path?page=7&per_page=1>; rel="next"', {"next": 7}),
        ('<https://api.github.com/x?per_page=9>; rel="next"', {}),  # per_page is not page
        ('<https://api.github.com/x?page=0>; rel="next"', {}),
        ('<https://api.github.com/x?page=2&page=3>; rel="next"', {"next": 2}),
        ('<https://api.github.com/x?page=abc>; rel="next", <y?page=4>; rel="last"', {"last": 4}),
        ("", {}),
        (None, {}),
    ],
)
def test_only_the_page_number_is_taken_from_a_link(link, expected):
    assert rest.link_pages(link) == expected


def limited(status, body="", **headers):
    return rest.Response(
        status, {key.replace("_", "-"): value for key, value in headers.items()}, body
    )


def test_an_exhausted_quota_holds_the_next_request_until_reset():
    clock = FakeClock()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=600)
    page = limited(200, x_ratelimit_remaining="0", x_ratelimit_reset=str(clock.now + 120))
    assert limiter.observe(page) is False  # the page itself is fine
    limiter.before({"kind": "rest"})
    assert clock.slept == [120]
    assert limiter.record()["waits"][0]["reason"] == "primary"
    limiter.before({"kind": "rest"})
    assert clock.slept == [120]  # a hold is used once


@pytest.mark.parametrize(
    "response, wait, reason",
    [
        (limited(403, "secondary rate limit", retry_after="30"), 30, "secondary"),
        (limited(429, retry_after="5"), 5, "secondary"),
        (limited(403, x_ratelimit_remaining="0", x_ratelimit_reset="1767225900"), 300, "primary"),
        (limited(403, '{"message": "You have exceeded a secondary rate limit"}'), 60, "secondary"),
    ],
)
def test_limit_refusals_are_waited_out_and_retried(response, wait, reason):
    clock = FakeClock()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=600)
    calls = iter([response, rest.Response(200, {}, "[]")])
    result = rest.fetch(limiter, {"kind": "rest"}, lambda: next(calls))
    assert result.status == 200
    assert clock.slept == [wait]
    assert limiter.waits[0]["reason"] == reason


def test_a_plain_forbidden_is_not_a_rate_limit():
    clock = FakeClock()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=600)
    response = rest.fetch(
        limiter, {}, lambda: limited(403, '{"message": "Resource not accessible"}')
    )
    assert response.status == 403 and clock.slept == []
    assert rest.failure_reason(response) == "http_403"


def test_waiting_past_the_cap_stops_with_partial():
    clock = FakeClock()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=40)
    calls = iter([limited(403, retry_after="30"), limited(403, retry_after="30")])
    with pytest.raises(rest.WaitLimitExceeded) as stopped:
        rest.fetch(limiter, {}, lambda: next(calls))
    assert stopped.value.exit_code == 1
    assert clock.slept == [30]


def test_a_limit_that_never_lifts_gives_up_after_the_retries():
    clock = FakeClock()
    limiter = rest.Limiter(clock, clock.sleep, max_wait=10_000)
    response = rest.fetch(limiter, {}, lambda: limited(429, retry_after="1"))
    assert response.status == 429 and len(clock.slept) == rest.MAX_RETRIES
    assert rest.failure_reason(response) == "rate_limited"


@pytest.fixture
def started(monkeypatch):
    seen = []

    def run(argv, **options):
        seen.append(SimpleNamespace(argv=argv, **options))
        return SimpleNamespace(returncode=0, stdout="HTTP/2.0 200 OK\r\n\r\n{}", stderr="x")

    monkeypatch.setattr(guard, "_run", run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    return seen


def test_the_real_transport_goes_through_the_guard_only(started):
    transport = rest.GhTransport(TOKEN, env={"PATH": "/usr/bin"})
    assert TOKEN not in repr(transport)
    values = {"owner": "example-org", "repo": "demo", "number": 3}
    assert transport.rest("pull_reviews", values, {"page": 2, "per_page": 100}).data() == {}
    assert transport.graphql(
        "review_threads", {"owner": "example-org", "name": "demo", "number": 3}
    ).ok
    rest_call, graphql_call = started
    assert rest_call.argv[:6] == ["/usr/bin/gh", "api", "--method", "GET", "-i", "-H"]
    assert rest_call.argv[-1] == "repos/example-org/demo/pulls/3/reviews?page=2&per_page=100"
    assert graphql_call.argv[:4] == ["/usr/bin/gh", "api", "graphql", "-i"]
    for call in started:
        assert TOKEN not in " ".join(call.argv)
        assert call.env["GH_TOKEN"] == TOKEN
