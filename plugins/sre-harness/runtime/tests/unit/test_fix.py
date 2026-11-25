"""Linking a review thread to the commit that fixed it, from collected raw pages only."""

import pytest

from examples.synthetic.github_responses import (
    TIMEOUT_PATCH,
    GitHubFixture,
    add_timeout_prs,
    at,
    sha,
)
from sre_harness import records
from sre_harness.review import fix
from tests.helpers import prepared_run, read_jsonl, run_dir


def linked(tmp_path, home, fixture):
    directory = run_dir(home, prepared_run(tmp_path, home, fixture))
    threads = fix.attach(directory, read_jsonl(directory / "threads.jsonl"))
    for thread in threads:
        assert records.violations("review-thread", thread) == []
    return {
        thread["pr"]["number"]: thread for thread in threads if thread["kind"] == "inline_thread"
    }, threads


@pytest.fixture
def timeout(tmp_path, home, clock):
    fixture = GitHubFixture()
    add_timeout_prs(fixture)
    return linked(tmp_path, home, fixture)


def test_an_applied_suggestion_is_found_with_the_changed_lines(timeout):
    found = timeout[0][11]["fix"]
    assert found["status"] == "found" and found["method"] == "suggestion_applied"
    assert found["commits"] == [sha("commit", 11, 2)]  # "Apply suggestions from code review"
    assert found["before"] == {
        "sha": sha("commit", 11, 1),
        "path": "src/client.py",
        "range": [12, 12],
    }
    assert found["after"] == {
        "sha": sha("commit", 11, 2),
        "path": "src/client.py",
        "range": [11, 11],
    }


@pytest.mark.parametrize("number", [12, 14, 16])
def test_a_reply_naming_a_commit_links_it(timeout, number):
    found = timeout[0][number]["fix"]
    assert found["status"] == "found" and found["method"] == "reply_link"
    assert found["commits"] == [sha("commit", number, 2)]  # not the changelog commit after it


@pytest.mark.parametrize("number", [13, 15])
def test_a_single_later_commit_on_the_path_is_found(timeout, number):
    found = timeout[0][number]["fix"]
    assert found["status"] == "found" and found["method"] == "same_path_later_commit"
    assert found["commits"] == [sha("commit", number, 2)]


def test_only_inline_threads_get_a_fix(timeout):
    _, threads = timeout
    assert {thread["kind"] for thread in threads if "fix" not in thread} == {"review_summary"}


def pull_with_thread(fixture, number, reachable=True, path_changed=True):
    pull = fixture.pull(number, updated_at=at(5, 50), created_at=at(5))
    start = pull.commit("Start", committed_at=at(5, 1))
    thread = pull.thread(
        path="src/client.py", line=12, original_commit=start if reachable else None
    )
    asked = thread.comment("bob", "This HTTP client has no timeout.", created_at=at(5, 10))
    if path_changed:
        pull.file("src/client.py", TIMEOUT_PATCH)
    return pull, thread, asked


def test_several_later_commits_without_a_signal_are_ambiguous(tmp_path, home, clock):
    fixture = GitHubFixture()
    pull, _, _ = pull_with_thread(fixture, 5)
    pull.commit("One", committed_at=at(5, 20))
    pull.commit("Two", committed_at=at(5, 30))
    result = linked(tmp_path, home, fixture)[0][5]["fix"]
    assert result["status"] == "ambiguous" and result["method"] == "same_path_later_commit"
    assert result["commits"] == sorted([sha("commit", 5, 2), sha("commit", 5, 3)])
    assert "after" not in result


def test_a_reply_naming_two_commits_is_ambiguous(tmp_path, home, clock):
    fixture = GitHubFixture()
    pull, thread, asked = pull_with_thread(fixture, 5)
    first = pull.commit("One", committed_at=at(5, 20))
    second = pull.commit("Two", committed_at=at(5, 30))
    thread.comment(
        "alice", f"See {first[:8]} and {second[:8]}.", created_at=at(5, 40), reply_to=asked
    )
    result = linked(tmp_path, home, fixture)[0][5]["fix"]
    assert result["status"] == "ambiguous" and result["method"] == "reply_link"
    assert result["commits"] == sorted([first, second])


def test_a_force_pushed_original_commit_is_not_found_and_listed(tmp_path, home, clock):
    fixture = GitHubFixture()
    pull, _, _ = pull_with_thread(fixture, 5, reachable=False)
    pull.commit("Rewritten", committed_at=at(5, 20))
    thread = linked(tmp_path, home, fixture)[0][5]
    assert thread["fix"] == {"status": "not_found", "commits": []}
    assert "fix.before" in thread["missing"]


def test_no_later_commit_on_the_path_is_not_found(tmp_path, home, clock):
    fixture = GitHubFixture()
    pull, _, _ = pull_with_thread(fixture, 5, path_changed=False)
    pull.commit("Elsewhere", committed_at=at(5, 20))
    thread = linked(tmp_path, home, fixture)[0][5]
    assert thread["fix"]["status"] == "not_found" and thread["fix"]["commits"] == []
    assert thread["fix"]["before"]["sha"] == sha("commit", 5, 1)
    assert "fix.before" not in thread["missing"]


def test_a_commit_before_the_comment_does_not_count(tmp_path, home, clock):
    fixture = GitHubFixture()
    pull, _, _ = pull_with_thread(fixture, 5)
    pull.commit("Earlier", committed_at=at(5, 5))
    assert linked(tmp_path, home, fixture)[0][5]["fix"]["status"] == "not_found"
