import json

import pytest

from examples.synthetic.github_responses import GitHubFixture, main
from sre_harness.errors import BoundaryError, InputError
from sre_harness.github import rest
from sre_harness.github.fixture import FixtureTransport, graphql_key, rest_key

REPO = {"owner": "example-org", "repo": "demo"}
LISTING = {"direction": "desc", "page": 1, "per_page": 100, "sort": "updated", "state": "all"}


def write(directory, responses, files):
    directory.mkdir()
    (directory / "index.json").write_text(json.dumps({"responses": responses}))
    for name, text in files.items():
        (directory / name).write_text(text)
    return directory


def test_answers_have_the_shape_gh_prints_and_repeat_the_last(tmp_path):
    key = rest_key("pulls", REPO, LISTING)
    directory = write(
        tmp_path / "fx",
        {key: ["a.http", "b.http"]},
        {
            "a.http": "HTTP/2.0 429 Too Many\r\nRetry-After: 1\r\n\r\n{}",
            "b.http": "HTTP/2.0 200 OK\r\n\r\n[]",
        },
    )
    transport = FixtureTransport(directory)
    statuses = [transport.rest("pulls", REPO, LISTING).status for _ in range(3)]
    assert statuses == [429, 200, 200]
    assert transport.requests == [key, key, key]


def test_an_unknown_request_is_an_input_error_not_a_guess(tmp_path):
    transport = FixtureTransport(write(tmp_path / "fx", {}, {}))
    with pytest.raises(InputError, match="no response"):
        transport.rest("pulls", REPO, LISTING)


def test_keys_go_through_the_same_guard_as_real_requests(tmp_path):
    transport = FixtureTransport(write(tmp_path / "fx", {}, {}))
    with pytest.raises(BoundaryError):
        transport.rest("merge", REPO, {})
    with pytest.raises(BoundaryError):
        transport.graphql("resolve_thread", {})
    assert graphql_key("review_threads", {"owner": "example-org", "name": "demo", "number": 2}) == (
        "graphql:review_threads?name=demo&number=2&owner=example-org"
    )


@pytest.mark.parametrize(
    "index",
    [
        "not json",
        json.dumps({"other": {}}),
        json.dumps({"responses": {"k": "a.http"}}),
        json.dumps({"responses": {"k": []}}),
        json.dumps({"responses": {"k": ["../outside.http"]}}),
        json.dumps({"responses": []}),
    ],
)
def test_a_malformed_index_is_refused(tmp_path, index):
    directory = tmp_path / "fx"
    directory.mkdir()
    (directory / "index.json").write_text(index)
    with pytest.raises(InputError):
        FixtureTransport(directory)


def test_the_fixture_directory_must_exist(tmp_path):
    with pytest.raises(InputError):
        FixtureTransport(tmp_path / "absent")


def test_a_listed_file_that_is_absent_or_too_large_stops(tmp_path, monkeypatch):
    key = rest_key("pulls", REPO, LISTING)
    directory = write(tmp_path / "fx", {key: ["gone.http"]}, {})
    with pytest.raises(InputError, match="missing"):
        FixtureTransport(directory).rest("pulls", REPO, LISTING)
    (directory / "gone.http").write_text("HTTP/2.0 200 OK\r\n\r\n" + "x" * 200)
    monkeypatch.setattr(rest, "MAX_RESPONSE_BYTES", 100)
    monkeypatch.setattr("sre_harness.github.fixture.MAX_RESPONSE_BYTES", 100)
    with pytest.raises(rest.ResponseTooLarge):
        FixtureTransport(directory).rest("pulls", REPO, LISTING)


def test_the_builder_pages_and_links_listings(tmp_path):
    fixture = GitHubFixture(page_size=2, link_host="evil.invalid", link_path="elsewhere/pulls")
    for number in range(1, 6):
        fixture.pull(number, updated_at=f"2026-01-{10 + number:02d}T00:00:00Z")
    transport = FixtureTransport(fixture.write(tmp_path / "fx"))
    first = transport.rest("pulls", REPO, LISTING)
    assert [item["number"] for item in first.data()] == [5, 4]
    assert "evil.invalid/elsewhere/pulls" in first.headers["link"]
    assert rest.link_pages(first.headers["link"]) == {"next": 2, "last": 3}


def test_the_script_writes_the_default_scenario(tmp_path, capsys):
    main([str(tmp_path / "fx")])
    assert json.loads(capsys.readouterr().out)["now"] == "2026-01-01T00:00:00Z"
    assert (tmp_path / "fx" / "index.json").is_file()
