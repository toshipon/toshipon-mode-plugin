import json

import pytest

from examples.synthetic.github_responses import MISSING, GitHubFixture
from sre_harness import records
from tests.helpers import collect_fixture, read_jsonl, run_cli, run_dir


def normalized(tmp_path, home, fixture, expect_collect=0):
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == expect_collect
    code, result = run_cli(["review", "normalize", "--run", summary["run_id"]], home)
    assert code == 0
    directory = run_dir(home, summary["run_id"])
    threads = read_jsonl(directory / "threads.jsonl")
    for thread in threads:
        assert records.violations("review-thread", thread) == []
    return result, threads, read_jsonl(directory / "rejected.jsonl"), directory


def by_kind(threads, kind):
    return [thread for thread in threads if thread["kind"] == kind]


def test_a_comment_from_rest_and_graphql_is_one_comment(tmp_path, home, clock):
    fixture = GitHubFixture()
    thread = fixture.pull(1).thread(path="src/client.py", line=12)
    asked = thread.comment("bob", "No timeout here.")
    thread.comment("alice", "Fixed.", reply_to=asked, association="OWNER")
    _, threads, rejected, _ = normalized(tmp_path, home, fixture)
    (inline,) = by_kind(threads, "inline_thread")
    assert [comment["id"] for comment in inline["comments"]] == ["PRRC_ex001001", "PRRC_ex001002"]
    assert inline["node_id"] == thread.node_id
    assert inline["comments"][1]["in_reply_to"] == "PRRC_ex001001"
    assert inline["comments"][1]["author_association"] == "owner"
    assert inline["thread"]["diff_hunk"].startswith("@@")  # only REST carries the hunk
    assert inline["thread"]["is_resolved"] is False  # only GraphQL carries the state
    assert inline["missing"] == [] and rejected == []


def test_items_repeated_across_a_page_boundary_are_kept_once(tmp_path, home, clock):
    fixture = GitHubFixture(page_size=1, overlap=1)
    pr = fixture.pull(1)
    thread = pr.thread()
    for author in ("bob", "carol", "bob"):
        thread.comment(author, "Synthetic.")
    for author in ("carol", "alice"):
        pr.issue_comment(author, "Conversation.")
    pr.review("bob", "APPROVED", "Fine.")
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    assert len(by_kind(threads, "inline_thread")[0]["comments"]) == 3
    assert len(by_kind(threads, "issue_comment")) == 2
    assert len(by_kind(threads, "review_summary")) == 1
    assert len({thread["node_id"] for thread in threads}) == len(threads)


def test_a_ghost_author_is_a_ghost_not_a_missing_login(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1, author=None)
    pr.thread().comment(None, "Left before the merge.")
    pr.issue_comment(None, "Also gone.")
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    for thread in threads:
        assert thread["pr"]["author_type"] == "ghost" and "author_login" not in thread["pr"]
        comment = thread["comments"][0]
        assert comment["author_type"] == "ghost" and "author_login" not in comment
        assert not [gap for gap in thread["missing"] if "author" in gap]


def test_bots_and_suggestions_are_marked(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1)
    pr.thread().comment("lint-bot", "```suggestion\nretries = 3\ntimeout = 10\n```", bot=True)
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    comment = threads[0]["comments"][0]
    assert comment["author_type"] == "bot"
    assert comment["suggestion"] == {"lines": ["retries = 3", "timeout = 10"]}


def test_absent_fields_are_listed_not_filled(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1, head_repo=None)  # a deleted fork
    pr.thread(resolved=MISSING, outdated=MISSING).comment("bob", "x", association=MISSING)
    _, threads, rejected, _ = normalized(tmp_path, home, fixture)
    (inline,) = threads
    assert "is_resolved" not in inline["thread"] and "is_outdated" not in inline["thread"]
    assert "head_repo" not in inline["pr"]
    assert "author_association" not in inline["comments"][0]
    assert {
        "thread.is_resolved",
        "thread.is_outdated",
        "pr.head_repo",
        "comments[0].author_association",
    } <= set(inline["missing"])
    assert rejected == []


def test_a_head_repo_key_left_out_is_listed_too(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1, head_repo=MISSING).issue_comment()
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    assert "pr.head_repo" in threads[0]["missing"]


def test_without_graphql_threads_come_from_rest_and_say_what_is_missing(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1)
    thread = pr.thread()
    asked = thread.comment("bob", "First.")
    reply = thread.comment("alice", "Second.", reply_to=asked)
    thread.comment("bob", "Third.", reply_to=reply)
    pr.thread(path="src/other.py").comment("carol", "Separate thread.")
    fixture.write(tmp_path / "fx")
    index = json.loads((tmp_path / "fx" / "index.json").read_text())
    for key in [key for key in index["responses"] if key.startswith("graphql:")]:
        (tmp_path / "fx" / index["responses"][key][0]).write_text("HTTP/2.0 502 Bad\r\n\r\n")
    code, summary = collect_fixture(tmp_path / "fx", home)
    assert code == 1
    run_cli(["review", "normalize", "--run", summary["run_id"]], home)
    threads = read_jsonl(run_dir(home, summary["run_id"]) / "threads.jsonl")
    inline = by_kind(threads, "inline_thread")
    assert [len(thread["comments"]) for thread in inline] == [3, 1]
    assert inline[0]["node_id"] == asked.node_id  # the root comment stands in for the thread
    assert inline[0]["comments"][2]["in_reply_to"] == reply.node_id
    for thread in inline:
        assert "is_resolved" not in thread["thread"]
        assert {"thread.is_resolved", "source.review_threads"} <= set(thread["missing"])
        assert records.violations("review-thread", thread) == []


def test_comments_past_the_first_graphql_page_are_joined(tmp_path, home, clock):
    fixture = GitHubFixture(comment_page_size=2, thread_page_size=1)
    pr = fixture.pull(1)
    for _ in range(2):
        thread = pr.thread()
        for index in range(5):
            thread.comment("bob" if index % 2 else "alice", f"Comment {index}.")
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    assert [len(thread["comments"]) for thread in threads] == [5, 5]


def test_a_record_that_breaks_the_schema_is_rejected_with_reasons(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1)
    pr.review("bob", "ODD_STATE", "Body.")
    pr.issue_comment("carol", "Fine.")
    pr.issue_comments[0]["created_at"] = "yesterday"
    result, threads, rejected, _ = normalized(tmp_path, home, fixture)
    assert result["threads"] == 0 and result["rejected"] == 2
    reasons = {row["kind"]: " ".join(row["reasons"]) for row in rejected}
    assert "review/state" in reasons["review_summary"]
    assert "comments/0/created_at" in reasons["issue_comment"]
    assert all(row["locator"].startswith("raw/") for row in rejected)


def test_review_bodies_and_verdicts(tmp_path, home, clock):
    fixture = GitHubFixture()
    pr = fixture.pull(1)
    pr.review("bob", "CHANGES_REQUESTED", "Needs a timeout.")
    pr.review("bob", "COMMENTED", "")  # only a container for inline comments
    pr.review("bob", "APPROVED", "")
    pr.review("carol", "DISMISSED", "")
    _, threads, _, _ = normalized(tmp_path, home, fixture)
    states = [thread["review"]["state"] for thread in by_kind(threads, "review_summary")]
    assert sorted(states) == ["approved", "changes_requested", "dismissed"]
    assert [thread["id"] for thread in threads] == ["RT-0001", "RT-0002", "RT-0003"]


def test_an_empty_period_normalizes_to_no_threads(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1, updated_at="2025-01-01T00:00:00Z")
    result, threads, rejected, directory = normalized(tmp_path, home, fixture)
    assert result == {
        "run_id": directory.name,
        "collect_status": "complete",
        "threads": 0,
        "rejected": 0,
    }
    assert threads == [] and rejected == []


def test_a_changed_raw_page_is_refused(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1).issue_comment()
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    directory = run_dir(home, summary["run_id"])
    (directory / "raw" / "0001.json").write_text("{}")
    assert run_cli(["review", "normalize", "--run", summary["run_id"]], home)[0] == 2


@pytest.mark.parametrize("run_id", ["../x", "run-20260101T000000Z-00000000"])
def test_normalize_opens_runs_by_id_only(home, run_id):
    assert run_cli(["review", "normalize", "--run", run_id], home)[0] == 2


def test_an_invalid_collect_record_is_refused(tmp_path, home, clock):
    fixture = GitHubFixture()
    fixture.pull(1)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    path = run_dir(home, summary["run_id"]) / "collect.json"
    record = json.loads(path.read_text())
    record["status"] = "done"
    path.write_text(json.dumps(record))
    assert run_cli(["review", "normalize", "--run", summary["run_id"]], home)[0] == 2
