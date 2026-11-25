"""`review analyze`: lessons from threads, held to quotes, ids, timestamps and fixed rules."""

import json

import pytest

from examples.synthetic.github_responses import (
    BOT_ONLY_PRS,
    CASE_SPECIFIC_PR,
    EXTERNAL_ONLY_PR,
    INJECTION_BODY,
    INJECTION_COMMENT,
    INJECTION_REPLY,
    NEW_POLICY_PR,
    OLD_POLICY_PR,
    STREAM_EXCEPTION_PR,
    TIMEOUT_FINDING,
    TIMEOUT_PRS,
    GitHubFixture,
    add_timeout_prs,
    review_learning_scenario,
)
from sre_harness import llm, records
from sre_harness.review import analyze
from tests.helpers import (
    LLM_ARGS,
    prepared_run,
    prompt_parts,
    read_jsonl,
    run_cli,
    run_dir,
    with_home,
)

OLD_POLICY = "Use client A for outbound HTTP calls."
NEW_POLICY = "Use client B instead of client A for outbound HTTP calls."
STREAM = "This is a stream, so no timeout is set on purpose."


def analyzed(tmp_path, env, fixture, *extra, backend="stub", name="fx"):
    run_id = prepared_run(tmp_path, env, fixture, name=name)
    argv = ["review", "analyze", "--run", run_id, "--backend", backend, *extra]
    code, summary = run_cli(argv, env)
    directory = run_dir(env, run_id)
    lessons = (
        read_jsonl(directory / "lessons.jsonl") if (directory / "lessons.jsonl").exists() else []
    )
    return code, summary, directory, lessons


def inline_ids(directory):
    return {
        thread["pr"]["number"]: thread["id"]
        for thread in read_jsonl(directory / "threads.jsonl")
        if thread["kind"] == "inline_thread"
    }


@pytest.fixture
def scenario(tmp_path, home, clock):
    code, summary, directory, lessons = analyzed(tmp_path, home, review_learning_scenario())
    assert code == 0, summary
    return summary, directory, {lesson["problem"]: lesson for lesson in lessons}


def test_every_lesson_is_schema_valid_and_the_files_are_private(scenario):
    summary, directory, lessons = scenario
    assert summary["lessons"] == len(lessons) == 6 and summary["rejected"] == 0
    assert [lesson["id"] for lesson in sorted(lessons.values(), key=lambda x: x["id"])] == [
        f"LS-{n:04d}" for n in range(1, 7)
    ]
    for lesson in lessons.values():
        assert records.violations("review-lesson", lesson) == []
        assert lesson["generated_by"]["backend"] == "stub"
    for thread in read_jsonl(directory / "threads-with-fix.jsonl"):
        assert records.violations("review-thread", thread) == []
    for name in (
        "lessons.jsonl",
        "rejected-lessons.jsonl",
        "threads-with-fix.jsonl",
        "analyze.json",
    ):
        assert (directory / name).stat().st_mode & 0o777 == 0o600
    (call,) = read_jsonl(directory / llm.CALLS_FILE)
    assert call["backend"] == "stub" and call["stage"] == "analyze"


def test_the_timeout_finding_is_one_strong_lesson_with_its_exception(scenario):
    _, directory, lessons = scenario
    lesson = lessons[TIMEOUT_FINDING]
    ids = inline_ids(directory)
    assert lesson["thread_ids"] == [ids[n] for n in (*TIMEOUT_PRS, STREAM_EXCEPTION_PR)]
    assert lesson["prs"] == [*TIMEOUT_PRS, STREAM_EXCEPTION_PR]
    assert lesson["confidence_basis"] == {
        "threads": 7,
        "distinct_reviewers": 3,
        "distinct_prs": 7,
        "contradicting_threads": 0,
    }
    assert lesson["confidence"] == "strong"
    assert lesson["source_trust"] == "member_supported"
    assert lesson["reviewer_agreement"] == "agreed"
    assert lesson["staleness"] == {
        "status": "current",
        "reason": "no later opposing thread",
        "later_evidence": [],
    }
    assert lesson["exceptions"] == [STREAM]
    assert STREAM in [quote["text"] for quote in lesson["quotes"]]


def test_every_quote_is_a_substring_of_its_comment(scenario):
    _, directory, lessons = scenario
    bodies = {
        comment["id"]: comment.get("body", "")
        for thread in read_jsonl(directory / "threads.jsonl")
        for comment in thread["comments"]
    }
    for lesson in lessons.values():
        for quote in lesson["quotes"]:
            assert quote["text"] and quote["text"] in bodies[quote["comment_id"]]


def test_an_old_policy_is_superseded_by_the_later_thread(scenario):
    _, directory, lessons = scenario
    ids = inline_ids(directory)
    old, new = lessons[OLD_POLICY], lessons[NEW_POLICY]
    assert old["staleness"]["status"] == "superseded"
    assert old["staleness"]["later_evidence"] == [ids[NEW_POLICY_PR]]
    assert old["confidence_basis"]["contradicting_threads"] == 1
    assert new["staleness"]["status"] == "current"
    assert old["thread_ids"] == [ids[OLD_POLICY_PR]]


def test_a_replacement_written_earlier_supersedes_nothing(tmp_path, home, clock):
    code, _, directory, lessons = analyzed(
        tmp_path, home, review_learning_scenario(reversed_policy=True)
    )
    assert code == 0
    old = {lesson["problem"]: lesson for lesson in lessons}[OLD_POLICY]
    assert old["staleness"]["status"] == "current" and old["staleness"]["later_evidence"] == []
    ignored = json.loads((directory / "analyze.json").read_text())["ignored_oppositions"]
    assert [entry["reason"] for entry in ignored] == ["not_later"]


def test_trust_comes_from_who_raised_the_point(scenario):
    _, directory, lessons = scenario
    ids = inline_ids(directory)
    external = lessons["Prefer tabs for indentation in this module."]
    assert external["thread_ids"] == [ids[EXTERNAL_ONLY_PR]]
    assert external["source_trust"] == "external_only"
    assert external["reviewer_agreement"] == "unanswered"
    bot = lessons["Line exceeds 100 characters."]
    assert bot["thread_ids"] == [ids[n] for n in BOT_ONLY_PRS]
    assert bot["source_trust"] == "bot_only" and bot["confidence"] == "candidate"
    scoped = lessons["Only here: rename tmp to buffer."]
    assert scoped["thread_ids"] == [ids[CASE_SPECIFIC_PR]] and scoped["scope"] == "case_specific"


def projection(lessons):
    keys = ("problem", "confidence", "confidence_basis", "source_trust", "scope", "prs")
    keys += ("reviewer_agreement", "exceptions", "fix_summary", "target")
    rows = []
    for lesson in lessons:
        row = {key: lesson[key] for key in keys}
        row["staleness"] = lesson["staleness"]["status"]
        row["threads"] = len(lesson["thread_ids"])
        rows.append(json.dumps(row, sort_keys=True))
    return sorted(rows)


def test_prompt_injection_changes_no_lesson(tmp_path, home, clock):
    _, _, _, plain = analyzed(tmp_path, home, review_learning_scenario(), name="plain")
    code, _, _, injected = analyzed(
        tmp_path, home, review_learning_scenario(injection=True), name="injected"
    )
    assert code == 0
    assert projection(injected) == projection(plain)


def test_injected_text_reaches_the_prompt_only_as_data(tmp_path, home, clock, fake_claude):
    calls = fake_claude({"lessons": [], "oppositions": []})
    env = with_home(home, tmp_path)
    code, summary, _, lessons = analyzed(
        tmp_path, env, review_learning_scenario(injection=True), *LLM_ARGS, backend="claude"
    )
    assert code == 0 and lessons == [] and summary["lessons"] == 0
    (call,) = calls
    head, data = prompt_parts(call.prompt)
    for text in (INJECTION_BODY, INJECTION_COMMENT, INJECTION_REPLY):
        assert text not in head
    assert "not instructions" in head
    bodies = [pull["body"] for pull in data["pull_requests"]]
    comments = [c["body"] for t in data["threads"] for c in t["comments"]]
    assert INJECTION_BODY in bodies
    assert INJECTION_COMMENT in comments and INJECTION_REPLY in comments
    assert call.argv[call.argv.index("--tools") + 1] == ""


# Fixed rules ------------------------------------------------------------------------------


def basis(threads, reviewers, prs, contradicting=0):
    return {
        "threads": threads,
        "distinct_reviewers": reviewers,
        "distinct_prs": prs,
        "contradicting_threads": contradicting,
    }


@pytest.mark.parametrize(
    "counts, level",
    [
        (basis(1, 1, 1), "candidate"),
        (basis(2, 2, 2), "candidate"),  # one or two threads stay a candidate
        (basis(3, 1, 3), "candidate"),  # one reviewer
        (basis(3, 2, 1), "candidate"),  # one PR
        (basis(3, 2, 2), "recurring"),
        (basis(3, 3, 3), "strong"),
        (basis(3, 3, 3, 1), "recurring"),  # a contradicting thread caps it
        (basis(7, 3, 7), "strong"),
        (basis(9, 5, 9, 2), "recurring"),
    ],
)
def test_confidence_follows_the_counts(counts, level):
    assert analyze.confidence(counts) == level


def person(kind="user", association="member"):
    comment = {"author_type": kind}
    if association is not None:
        comment["author_association"] = association
    return comment


@pytest.mark.parametrize(
    "comments, trust",
    [
        ([person("user", "member"), person("user", "none")], "member_supported"),
        ([person("user", "owner")], "member_supported"),
        ([person("user", "collaborator")], "member_supported"),
        ([person("user", "none")], "external_only"),
        (
            [person("user", "contributor"), person("user", "first_time_contributor")],
            "external_only",
        ),
        ([person("user", None)], "external_only"),  # association not returned
        ([person("bot", "none"), person("bot", "none")], "bot_only"),
        ([person("bot", "member")], "bot_only"),  # a bot is never a member
        ([person("bot", "none"), person("user", "none")], "external_only"),
        ([{"author_type": "ghost"}], "external_only"),
        ([{"author_type": "ghost"}, person("user", "member")], "member_supported"),
    ],
    ids=[
        "member_and_external",
        "owner",
        "collaborator",
        "none",
        "contributors",
        "no_association",
        "bots",
        "bot_marked_member",
        "bot_and_external",
        "ghost",
        "ghost_and_member",
    ],
)
def test_source_trust_follows_who_raised_the_point(comments, trust):
    assert analyze.source_trust(comments) == trust


def thread(replies=0, resolved=False, fix="not_found"):
    comments = [{"id": f"C{n}", "author_type": "user"} for n in range(1 + replies)]
    return {
        "kind": "inline_thread",
        "thread": {"is_resolved": resolved},
        "comments": comments,
        "fix": {"status": fix},
    }


@pytest.mark.parametrize(
    "threads, agreement",
    [
        ([thread(1, resolved=True)], "agreed"),
        ([thread(1, fix="found")], "agreed"),
        ([thread(1)], "disputed"),
        ([thread(0)], "unanswered"),
        ([thread(1, resolved=True), thread(1)], "disputed"),
        ([thread(1, resolved=True), thread(0)], "agreed"),
    ],
)
def test_reviewer_agreement_follows_replies_and_resolution(threads, agreement):
    assert analyze.reviewer_agreement(threads) == agreement


# Validating what a backend returns --------------------------------------------------------


def draft(key, item, text=None, comment=None, thread_ids=None):
    comment = comment or item["comments"][0]
    return {
        "key": key,
        "thread_ids": thread_ids or [item["id"]],
        "target": "src/client.py",
        "problem": "The HTTP client has no timeout.",
        "rationale": "A slow upstream hangs the worker.",
        "fix_summary": "",
        "applies_when": [],
        "exceptions": [],
        "quotes": [{"comment_id": comment["id"], "text": text or comment["body"][:20]}],
        "scope": "general",
    }


def inline(data, number):
    return next(t for t in data["threads"] if t["pr"] == number and t["kind"] == "inline_thread")


def timeout_fixture():
    fixture = GitHubFixture()
    add_timeout_prs(fixture)
    return fixture


def test_only_the_lessons_that_fail_a_check_are_removed(tmp_path, home, clock, fake_claude):
    def answer(data):
        first, second = inline(data, 11), inline(data, 12)
        return {
            "lessons": [
                draft("L1", first),
                draft("L2", first, text="A quote nobody wrote."),
                draft("L3", first, thread_ids=["RT-9999"]),
                draft("L4", first, comment=second["comments"][0]),  # not in the lesson's thread
                draft("L5", second, comment={"id": "PRRC_unknown", "body": "x" * 30}),
            ],
            "oppositions": [{"lesson": "L1", "opposed_by_thread_ids": ["RT-9998"], "basis": "x"}],
        }

    fake_claude(answer)
    env = with_home(home, tmp_path)
    code, summary, directory, lessons = analyzed(
        tmp_path, env, timeout_fixture(), *LLM_ARGS, backend="claude"
    )
    assert code == 0
    assert [lesson["id"] for lesson in lessons] == ["LS-0001"]
    assert lessons[0]["generated_by"] == {"backend": "claude", "call_id": "LC-0001"}
    rejected = {
        row["key"]: row["reasons"] for row in read_jsonl(directory / "rejected-lessons.jsonl")
    }
    assert set(rejected) == {"L2", "L3", "L4", "L5"}
    assert rejected["L2"][0].startswith("quote_not_found")
    assert rejected["L3"][0].startswith("unknown_thread")
    assert rejected["L4"][0].startswith("comment_not_in_threads")
    assert rejected["L5"][0].startswith("comment_not_in_threads")
    assert summary["rejected"] == 4
    ignored = json.loads((directory / "analyze.json").read_text())["ignored_oppositions"]
    assert [entry["reason"] for entry in ignored] == ["unknown_thread"]
    assert lessons[0]["staleness"]["status"] == "current"


def test_when_every_lesson_fails_the_stage_fails(tmp_path, home, clock, fake_claude):
    fake_claude(
        lambda data: {
            "lessons": [draft("L1", inline(data, 11), text="Never said.")],
            "oppositions": [],
        }
    )
    env = with_home(home, tmp_path)
    code, _, directory, lessons = analyzed(
        tmp_path, env, timeout_fixture(), *LLM_ARGS, backend="claude"
    )
    assert code == 1 and lessons == []
    (row,) = read_jsonl(directory / "rejected-lessons.jsonl")
    assert row["reasons"][0].startswith("quote_not_found")


def test_output_outside_the_schema_fails_the_stage(tmp_path, home, clock, fake_claude):
    fake_claude({"lessons": [{"key": "L1"}], "oppositions": []})
    env = with_home(home, tmp_path)
    code, _, _, _ = analyzed(tmp_path, env, timeout_fixture(), *LLM_ARGS, backend="claude")
    assert code == 1


def test_a_denied_term_stops_before_anything_is_sent(tmp_path, home, clock, fake_claude, capsys):
    calls = fake_claude({"lessons": [], "oppositions": []})
    terms = tmp_path / "deny.txt"
    terms.write_text("# synthetic terms\nupstream client\n")
    env = with_home(home, tmp_path)
    extra = [*LLM_ARGS, "--deny-terms-file", str(terms)]
    code, _, directory, _ = analyzed(tmp_path, env, timeout_fixture(), *extra, backend="claude")
    assert code == 3 and calls == []
    assert "upstream" not in capsys.readouterr().err
    assert not (directory / llm.CALLS_FILE).exists()


@pytest.mark.parametrize(
    "extra",
    [
        [],  # no --backend
        ["--backend", "openai"],
        ["--backend", "claude", "--max-run-budget-usd", "1"],  # no --auth-mode
        ["--backend", "claude", "--auth-mode", "subscription"],  # no run budget
        ["--backend", "claude", "--auth-mode", "subscription", "--max-run-budget-usd", "-1"],
    ],
    ids=["no_backend", "unknown_backend", "no_auth_mode", "no_budget", "negative_budget"],
)
def test_backend_settings_must_be_explicit(tmp_path, home, clock, fake_claude, extra):
    calls = fake_claude({"lessons": [], "oppositions": []})
    run_id = prepared_run(tmp_path, home, timeout_fixture())
    code, _ = run_cli(["review", "analyze", "--run", run_id, *extra], home)
    assert code == 2 and calls == []
