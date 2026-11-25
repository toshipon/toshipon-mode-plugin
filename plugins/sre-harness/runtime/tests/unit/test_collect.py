import json
import stat

import pytest

from examples.synthetic.github_responses import GitHubFixture, default_scenario
from sre_harness import guard, paths, records
from sre_harness.github import rest
from sre_harness.github.fixture import FixtureTransport
from sre_harness.review import collect
from tests.helpers import PERIOD, TOKEN, TOKEN_ENV, collect_fixture, run_cli, run_dir


def collected(env, summary):
    directory = run_dir(env, summary["run_id"])
    return directory, json.loads((directory / "collect.json").read_text())


def test_a_period_is_collected_newest_first_and_stops_past_since(tmp_path, home, clock):
    fixture = GitHubFixture(page_size=1)
    for number, day in ((1, 20), (2, 15), (3, 5)):
        fixture.pull(number, updated_at=f"2026-01-{day:02d}T00:00:00Z")
    fixture.pull(4, updated_at="2026-02-02T00:00:00Z")  # after --until: skipped
    fixture.pull(5, updated_at="2025-12-20T00:00:00Z")  # before --since: stops the listing
    fixture.pull(6, updated_at="2025-12-01T00:00:00Z")  # never requested
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 0 and summary["status"] == "complete"
    directory, record = collected(home, summary)
    assert records.violations("collect", record) == []
    assert record["pulls"] == [1, 2, 3]
    listing = [
        page["request"]["page"]
        for page in record["pages"]
        if page["request"].get("endpoint") == "pulls"
    ]
    assert listing == [1, 2, 3, 4, 5]  # page 6 (PR 6) is never read
    per_pr = {
        page["request"].get("endpoint") or page["request"]["query"]
        for page in record["pages"]
        if page["request"].get("number") == 2
    }
    assert per_pr == {*collect.PULL_ENDPOINTS, "review_threads"}
    assert record["page_count"] == len(record["pages"]) == 5 + 3 * 6


def test_raw_pages_are_private_and_named_by_the_harness(tmp_path, home, clock):
    code, summary = collect_fixture(default_scenario().write(tmp_path / "fx"), home)
    directory, record = collected(home, summary)
    names = sorted(path.name for path in (directory / "raw").iterdir())
    assert names == [page["file"] for page in record["pages"]]
    assert names == [f"{index:04d}.json" for index in range(1, len(names) + 1)]
    for path in (directory / "raw").iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        stored = json.loads(path.read_text())
        assert set(stored["headers"]) <= set(rest.KEPT_HEADERS)
        assert records.sha256(path.read_text()) == next(
            page["sha256"] for page in record["pages"] if page["file"] == path.name
        )


def test_next_links_elsewhere_yield_only_a_page_number(tmp_path, home, clock):
    fixture = GitHubFixture(page_size=1, link_host="evil.invalid", link_path="repos/evil/x/pulls")
    for number in (1, 2, 3):
        fixture.pull(number, updated_at=f"2026-01-1{number}T00:00:00Z")
    transport = FixtureTransport(fixture.write(tmp_path / "fx"))
    root = paths.state_root(home)
    directory = paths.create_run(root)
    limiter = rest.Limiter(clock, clock.sleep, 60)
    record = collect.collect(
        transport,
        limiter,
        directory,
        directory.name,
        "example-org/demo",
        "2026-01-01T00:00:00Z",
        "2026-01-31T00:00:00Z",
        {"kind": "fixture"},
        clock,
    )
    assert record["status"] == "complete" and record["pulls"] == [3, 2, 1]
    assert transport.requests
    for key in transport.requests:
        assert key.startswith(("repos/example-org/demo/", "graphql:"))
        assert "evil" not in key


def test_an_exhausted_quota_is_waited_on_the_injected_clock(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1)
    fixture.limit("pulls", kind="exhausted", reset_in=120)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 0 and summary["waited_seconds"] == 120
    assert clock.slept == [120]
    _, record = collected(home, summary)
    assert record["rate_limit"]["waits"][0]["reason"] == "primary"
    assert record["rate_limit"]["waits"][0]["at"] == "2026-01-01T00:00:00Z"


@pytest.mark.parametrize("kind, wait", [("secondary", 30), ("secondary429", 30), ("primary", 120)])
def test_a_refused_page_is_waited_out_and_read(tmp_path, home, clock, kind, wait):
    fixture = GitHubFixture()
    fixture.pull(1)
    fixture.limit("pull_review_comments", number=1, kind=kind, retry_after=30, reset_in=120)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 0 and summary["status"] == "complete"
    assert clock.slept == [wait]


def test_waiting_past_the_cap_is_partial_with_exit_1(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1)
    fixture.limit("pull_reviews", number=1, kind="secondary", retry_after=30)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home, "--max-wait", "10")
    assert code == 1
    assert summary["status"] == "partial" and summary["stopped"] == "wait_limit"
    assert clock.slept == []
    _, record = collected(home, summary)
    assert record["stopped"]["reason"] == "wait_limit"
    assert records.violations("collect", record) == []


def test_a_missing_middle_page_is_recorded_and_later_pages_still_read(tmp_path, home, clock):
    fixture = GitHubFixture(page_size=1)
    pr = fixture.pull(1)
    thread = pr.thread()
    for author in ("bob", "alice", "carol"):
        thread.comment(author, "Synthetic.")
    fixture.fail("pull_review_comments", page=2, number=1, status=404)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 1 and summary["status"] == "partial" and summary["missing"] == 1
    _, record = collected(home, summary)
    (gap,) = record["missing"]
    assert gap == {
        "request": {"kind": "rest", "endpoint": "pull_review_comments", "page": 2, "number": 1},
        "status": 404,
        "reason": "http_404",
    }
    pages = [
        page["request"]["page"]
        for page in record["pages"]
        if page["request"].get("endpoint") == "pull_review_comments"
    ]
    assert pages == [1, 3]


def test_a_failed_first_page_ends_that_listing(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1)
    fixture.fail("pulls", page=1, status=500)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 1 and summary["pulls"] == 0 and summary["missing"] == 1


def test_an_empty_period_completes_with_no_pulls(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1, updated_at="2025-06-01T00:00:00Z")
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 0 and summary["status"] == "complete" and summary["pulls"] == 0


def test_an_oversized_response_stops_the_run_as_partial(tmp_path, home, clock, monkeypatch):
    monkeypatch.setattr("sre_harness.github.fixture.MAX_RESPONSE_BYTES", 300)
    code, summary = collect_fixture(default_scenario().write(tmp_path / "fx"), home)
    assert code == 1 and summary["stopped"] == "response_too_large"


def test_an_empty_token_variable_starts_nothing(home, monkeypatch, tmp_path):
    started = []
    monkeypatch.setattr(guard, "_run", lambda *a, **k: started.append(a))
    argv = ["review", "collect", "--repo", "example-org/demo", *PERIOD, "--auth-env", TOKEN_ENV]
    for env in (home, {**home, TOKEN_ENV: ""}, {**home, TOKEN_ENV: "   ", "GH_TOKEN": "ambient"}):
        assert run_cli(argv, env) == (2, None)
    assert started == []
    assert not (tmp_path / "state" / "sre-harness" / "runs").exists()


def test_the_real_path_runs_only_gh_api_reads_and_stores_no_token(tmp_path, home, clock, fake_gh):
    started = fake_gh(default_scenario().write(tmp_path / "fx"))
    env = {**home, TOKEN_ENV: TOKEN}
    argv = ["review", "collect", "--repo", "example-org/demo", *PERIOD, "--auth-env", TOKEN_ENV]
    code, summary = run_cli(argv, env)
    assert code == 0 and summary["status"] == "complete"
    assert started
    for call in started:
        assert call.argv[:2] == ["/usr/bin/gh", "api"]
        assert call.argv[2:6] == ["--method", "GET", "-i", "-H"] or call.argv[2:4] == [
            "graphql",
            "-i",
        ]
        assert TOKEN not in " ".join(call.argv)
        assert call.env["GH_TOKEN"] == TOKEN
    directory, record = collected(env, summary)
    assert record["source"] == {"kind": "github", "auth_env": TOKEN_ENV}
    for path in directory.rglob("*"):
        if path.is_file():
            assert TOKEN not in path.read_text()


@pytest.mark.parametrize(
    "extra",
    [
        ["--repo", "example-org", *PERIOD, "--fixture", "x"],
        ["--repo", "example-org/..", *PERIOD, "--fixture", "x"],
        [
            "--repo",
            "example-org/demo",
            "--since",
            "2026-01-01",
            "--until",
            "2026-02-01T00:00:00Z",
            "--fixture",
            "x",
        ],
        [
            "--repo",
            "example-org/demo",
            "--since",
            "2026-02-01T00:00:00Z",
            "--until",
            "2026-01-01T00:00:00Z",
            "--fixture",
            "x",
        ],
        ["--repo", "example-org/demo", *PERIOD, "--fixture", "x", "--max-wait", "-1"],
        ["--repo", "example-org/demo", *PERIOD, "--fixture", "x", "--auth-env", TOKEN_ENV],
        ["--repo", "example-org/demo", *PERIOD],
        ["--repo", "example-org/demo", *PERIOD, "--fixture", "/nonexistent/fixture"],
    ],
)
def test_bad_input_is_exit_2(home, extra):
    assert run_cli(["review", "collect", *extra], home)[0] == 2


def rewrite(directory, prefix, text):
    """Replace the first stored answer whose request key starts with `prefix`."""
    index = json.loads((directory / "index.json").read_text())
    key = next(key for key in index["responses"] if key.startswith(prefix))
    (directory / index["responses"][key][0]).write_text(text)


def test_a_listing_page_that_is_not_json_is_missing(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1).issue_comment()
    directory = fixture.write(tmp_path / "fx")
    rewrite(directory, "repos/example-org/demo/issues/1/comments", "HTTP/2.0 200 OK\r\n\r\n<html>")
    rewrite(directory, "repos/example-org/demo/pulls/1/files", 'HTTP/2.0 200 OK\r\n\r\n{"a": 1}')
    code, summary = collect_fixture(directory, home)
    _, record = collected(home, summary)
    assert code == 1
    assert [gap["reason"] for gap in record["missing"]] == ["body_not_json", "unexpected_shape"]


def test_a_next_link_that_does_not_move_forward_is_not_followed(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1)
    directory = fixture.write(tmp_path / "fx")
    link = 'Link: <https://api.github.com/repos/example-org/demo/pulls?page=1>; rel="next"'
    rewrite(directory, "repos/example-org/demo/pulls?", f"HTTP/2.0 200 OK\r\n{link}\r\n\r\n[]")
    code, summary = collect_fixture(directory, home)
    _, record = collected(home, summary)
    assert code == 1 and record["missing"][0]["reason"] == "unusable_next_link"


def test_a_server_cursor_the_guard_refuses_stops_with_exit_3(tmp_path, home, clock):
    fixture = GitHubFixture(thread_page_size=1)
    pr = fixture.pull(1)
    pr.thread().comment()
    pr.thread().comment()
    directory = fixture.write(tmp_path / "fx")
    body = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": True, "endCursor": "not a cursor; $(x)"},
                        "nodes": [],
                    }
                }
            }
        }
    }
    rewrite(directory, "graphql:review_threads", "HTTP/2.0 200 OK\r\n\r\n" + json.dumps(body))
    code, _ = collect_fixture(directory, home)
    assert code == 3
    (run,) = (tmp_path / "state" / "sre-harness" / "runs").iterdir()
    record = json.loads((run / "collect.json").read_text())
    assert record["status"] == "partial" and record["stopped"]["reason"] == "boundary"
