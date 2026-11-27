"""The reference decision queue (`knowledge decision put / answer / poll / list / cancel`): files
under <state root>/knowledge/decisions/, no other tool. Everything is synthetic."""

import io
import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from sre_harness import cli, records
from sre_harness.knowledge import cli as knowledge_cli
from sre_harness.knowledge import decisions, model
from tests.invariants.test_boundaries import RUNTIME
from tests.knowledge_helpers import item, state_root, update, write_config, write_input

NOW = "2026-11-27T06:00:00Z"
LATER = "2026-11-28T06:00:00Z"
HEAD = "c" * 40
OTHER_HEAD = "d" * 40
QUESTION = "キューの保持期間を 14 日から 30 日に延ばしてよいか"
OPTIONS = ["延ばす", "延ばさない"]
GAMMA_SCOPE = {
    "level": "project",
    "project_id": "project-gamma",
    "component_id": None,
    "task_id": None,
}
R1 = model.record_id("synthetic", "R1")
R2 = model.record_id("synthetic", "R2")
G1 = model.record_id("synthetic", "G1")
ABSENT = "KN-000000000000"


@pytest.fixture
def clock(monkeypatch):
    moment = {"now": NOW}
    monkeypatch.setattr(knowledge_cli, "clock", lambda: moment["now"])
    return moment


def items(summary="R1: キューのメッセージ保持期間は 14 日にする"):
    gamma = item(
        "G1",
        scope=GAMMA_SCOPE,
        access={"classification": "confidential", "readers": ["team-gamma"]},
    )
    return [item("R1", summary=summary), item("R2"), gamma]


@pytest.fixture
def env(home, tmp_path, clock):
    write_config(home)
    code, _ = update(home, write_input(tmp_path / "in.json", items()))
    assert code == 0
    return home


def run(env, *argv):
    """(exit code, the JSON printed last or None, all of stdout)."""
    out = io.StringIO()
    code = cli.main(["knowledge", "decision", *argv], env=env, out=out)
    text = out.getvalue()
    start = 0 if text.startswith("{") else text.rfind("\n{\n") + 1
    value = json.loads(text[start:]) if text.strip() and (start or text.startswith("{")) else None
    return code, value, text


def task_file(tmp_path, workspace="ws-a", principal="workspace-a", task="TASK-0001"):
    path = tmp_path / f"task-{workspace}-{task}.json"
    value = {
        "schema_version": 1,
        "task_id": task,
        "workspace_id": workspace,
        "principal": principal,
        "project_id": "project-alpha",
        "component_id": None,
        "repo": "example-org/alpha",
        "base_sha": HEAD,
        "changed_paths": ["infra/queue/main.tf"],
        "resource_types": ["queue"],
    }
    path.write_text(json.dumps(value))
    return path


def put(env, tmp_path, *extra, question=QUESTION, evidence=(R1,), options=OPTIONS, **task):
    argv = ["put", "--task-file", str(task_file(tmp_path, **task)), "--question", question]
    for option in options:
        argv += ["--option", option]
    for record_id in evidence:
        argv += ["--evidence", record_id]
    code, value, _ = run(env, *argv, "--risk", "medium", "--head-sha", HEAD, *extra)
    if value is not None:
        assert records.violations("decision-request", value, "put_result") == [], value
    return code, value


TYPED = {}  # what the person types next: the confirmation prefix of the answer being given


def at_terminal(monkeypatch, typed=None):
    """A person at a terminal: the TTY probe says yes, and the prompt gets the first
    characters of the confirmation hash the screen shows (or `typed`)."""
    asked = []
    monkeypatch.setattr(knowledge_cli, "_is_terminal", lambda stream: True)

    def reply(prompt):
        asked.append(prompt)
        return typed if typed is not None else TYPED.get("value", "")

    monkeypatch.setattr("builtins.input", reply)
    return asked


def _expect(env, request_id, option, extra):
    """The prefix a person reads off the screen for this answer."""
    path = state_root(env) / "knowledge" / "decisions" / "outbox" / f"{request_id}.json"
    if not path.is_file():
        return
    flags = dict(zip(extra[::2], extra[1::2]))
    reuse = {"scope": flags.get("--reuse-scope", "task"), "until": flags.get("--reuse-until")}
    request = json.loads(path.read_text())
    TYPED["value"] = decisions.confirmation(request, option, reuse)[: decisions.CONFIRM_LENGTH]


def answer(env, request_id, option="延ばさない", responder="alice", *extra):
    _expect(env, request_id, option, extra)
    code, value, text = run(
        env, "answer", "--request", request_id, "--option", option, "--responder", responder, *extra
    )
    if code == 0:
        assert records.violations("decision-response", value) == [], value
    return code, value, text


def poll(env, workspace="ws-a", *extra):
    code, value, _ = run(env, "poll", "--workspace", workspace, *extra)
    if value is not None:
        assert records.violations("decision-request", value, "poll_result") == [], value
    return code, value


def decisions_dir(env):
    return state_root(env) / "knowledge" / "decisions"


# put ---------------------------------------------------------------------------------------


def test_put_writes_one_request_in_the_outbox_once(env, tmp_path):
    code, found = put(env, tmp_path)
    assert code == 0 and found["status"] == "accepted"
    request = found["request"]
    assert records.violations("decision-request", request) == []
    assert request["status"] == "open" and request["workspace_id"] == "ws-a"
    assert request["revision"]["head_sha"] == HEAD
    assert request["revision"]["generation"] == 1
    assert set(request["revision"]["content_hashes"]) == {R1}
    path = decisions_dir(env) / "outbox" / f"{request['id']}.json"
    assert json.loads(path.read_text()) == request
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    for folder in (decisions_dir(env), decisions_dir(env) / "outbox"):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_the_same_question_again_is_a_duplicate_of_the_first(env, tmp_path):
    _, first = put(env, tmp_path)
    before = sorted((decisions_dir(env) / "outbox").iterdir())
    code, again = put(
        env, tmp_path, question="  キューの保持期間を 14 日から 30 日に延ばしてよいか "
    )
    assert code == 0 and again["status"] == "duplicate"
    assert again["request"]["id"] == first["request"]["id"]
    assert sorted((decisions_dir(env) / "outbox").iterdir()) == before


def test_another_workspace_on_the_same_task_gets_the_same_request(env, tmp_path):
    _, first = put(env, tmp_path)
    code, other = put(env, tmp_path, workspace="ws-b", principal="workspace-b")
    assert code == 0 and other["status"] == "duplicate"
    assert other["request"]["id"] == first["request"]["id"]
    assert other["request"]["workspace_id"] == "ws-a"
    _, seen = poll(env, "ws-b")
    assert [r["id"] for r in seen["requests"]] == [first["request"]["id"]]


def test_another_task_another_question_or_another_head_is_another_request(env, tmp_path):
    _, first = put(env, tmp_path)
    ids = {first["request"]["id"]}
    _, other = put(env, tmp_path, task="TASK-0002")
    ids.add(other["request"]["id"])
    _, other = put(env, tmp_path, question="別の質問")
    ids.add(other["request"]["id"])
    code, other, _ = run(
        env,
        *["put", "--task-file", str(task_file(tmp_path)), "--question", QUESTION],
        *["--option", "a", "--option", "b", "--evidence", R1, "--risk", "low"],
        *["--head-sha", OTHER_HEAD],
    )
    assert code == 0
    ids.add(other["request"]["id"])
    assert len(ids) == 4


def test_evidence_the_principal_cannot_read_gets_the_answer_of_a_missing_id(env, tmp_path, capsys):
    capsys.readouterr()
    hidden = put(env, tmp_path, evidence=(G1,), workspace="ws-b", principal="workspace-b")
    hidden_error = capsys.readouterr().err
    absent = put(env, tmp_path, evidence=(ABSENT,), workspace="ws-b", principal="workspace-b")
    absent_error = capsys.readouterr().err
    assert hidden == absent == (2, None)
    assert hidden_error.replace(G1, "<id>") == absent_error.replace(ABSENT, "<id>")
    assert not (decisions_dir(env) / "outbox").exists() or not any(
        (decisions_dir(env) / "outbox").iterdir()
    )
    assert put(env, tmp_path, evidence=(G1,))[0] == 0  # workspace-a reads project-gamma


def test_put_with_an_unknown_principal_is_refused(env, tmp_path):
    assert put(env, tmp_path, principal="nobody") == (3, None)


@pytest.mark.parametrize(
    "question, options",
    [
        ("", OPTIONS),
        ("x" * 501, OPTIONS),
        ("鍵は sk-ant-" + "a" * 30 + " でよいか", OPTIONS),
        (QUESTION, ["一つだけ"]),
        (QUESTION, ["同じ", "同じ"]),
        (QUESTION, ["x" * 101, "y"]),
        (QUESTION, ["ghp_" + "a" * 36, "y"]),
        (QUESTION, [str(n) for n in range(11)]),
    ],
)
def test_a_question_or_options_outside_the_limits_are_refused(env, tmp_path, question, options):
    assert put(env, tmp_path, question=question, options=options) == (2, None)


def test_control_and_bidi_characters_are_dropped_from_the_question(env, tmp_path):
    _, found = put(env, tmp_path, question="保持期間を\x1b[31m延ば‮す\tか")
    assert found["request"]["question"] == "保持期間を[31m延ばす か"


def test_put_needs_evidence_and_a_risk(env, tmp_path):
    assert put(env, tmp_path, evidence=()) == (2, None)
    argv = ["put", "--task-file", str(task_file(tmp_path)), "--question", QUESTION]
    assert run(env, *argv, "--option", "a", "--option", "b", "--evidence", R1)[0] == 2


def test_concurrent_puts_in_threads_make_one_request(env, tmp_path):
    path = task_file(tmp_path)
    argv = ["knowledge", "decision", "put", "--task-file", str(path), "--question", QUESTION]
    argv += ["--option", "a", "--option", "b", "--evidence", R1, "--risk", "low"]
    answers, barrier = [], threading.Barrier(8)

    def one():
        out = io.StringIO()
        barrier.wait()
        code = cli.main(argv, env=env, out=out)
        answers.append((code, json.loads(out.getvalue())["status"]))

    threads = [threading.Thread(target=one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(answers) == [(0, "accepted")] + [(0, "duplicate")] * 7
    assert len(list((decisions_dir(env) / "outbox").iterdir())) == 1


def test_concurrent_puts_in_separate_processes_make_one_request(env, tmp_path, clock):
    workspaces = [f"ws-{n}" for n in range(4)]
    starts = []
    for workspace in workspaces:
        path = task_file(tmp_path, workspace=workspace)
        argv = [sys.executable, "-m", "sre_harness", "knowledge", "decision", "put"]
        argv += ["--task-file", str(path), "--question", QUESTION, "--option", "a"]
        argv += ["--option", "b", "--evidence", R1, "--risk", "low"]
        starts.append(
            subprocess.Popen(
                argv,
                cwd=RUNTIME,
                env={**os.environ, **env},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )
    outcomes = []
    for process in starts:
        out, err = process.communicate(timeout=60)
        assert process.returncode == 0, err
        outcomes.append(json.loads(out)["status"])
    assert sorted(outcomes) == ["accepted", "duplicate", "duplicate", "duplicate"]
    assert len(list((decisions_dir(env) / "outbox").iterdir())) == 1


def test_a_symlinked_decisions_directory_is_refused(env, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    decisions_dir(env).symlink_to(target)
    assert put(env, tmp_path) == (3, None)
    assert list(target.iterdir()) == []


# answer ------------------------------------------------------------------------------------


def test_answer_needs_a_terminal_and_writes_nothing_without_one(env, tmp_path):
    _, found = put(env, tmp_path)
    code, value, _ = answer(env, found["request"]["id"])
    assert (code, value) == (3, None)
    assert not (decisions_dir(env) / "inbox").exists()


def test_at_a_terminal_the_answer_is_written_once_to_the_inbox(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    request_id = found["request"]["id"]
    asked = at_terminal(monkeypatch)
    code, response, text = answer(env, request_id)
    assert code == 0 and asked and "confirmation_sha256" in asked[0]
    assert text.startswith(f"# Answer {request_id}")
    assert QUESTION in text and "延ばす" in text
    assert response == {
        "schema_version": 1,
        "request_id": request_id,
        "option": "延ばさない",
        "responder": "alice",
        "answered_at": NOW,
        "reuse": {"scope": "task", "until": None},
    }
    path = decisions_dir(env) / "inbox" / f"{request_id}.json"
    assert json.loads(path.read_text()) == response
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    code, _, _ = answer(env, request_id, "延ばす", "bob")
    assert code == 1 and path.read_bytes() == before


def test_a_mistyped_confirmation_records_nothing(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    at_terminal(monkeypatch, typed="000000000000")
    assert answer(env, found["request"]["id"])[0] == 3
    assert not (decisions_dir(env) / "inbox").exists()


def test_an_option_that_was_not_offered_is_an_input_error(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    at_terminal(monkeypatch)
    assert answer(env, found["request"]["id"], "どちらでもない")[0] == 2
    assert not (decisions_dir(env) / "inbox").exists()


@pytest.mark.parametrize(
    "name",
    ["Claude", "claude-code", "ClaudeCode", "GPT4", "review-bot", "AI", "my agent", "the harness"],
)
def test_an_ai_or_automation_name_cannot_answer(env, tmp_path, monkeypatch, name):
    _, found = put(env, tmp_path)
    at_terminal(monkeypatch)
    assert answer(env, found["request"]["id"], "延ばさない", name)[0] == 3
    assert not (decisions_dir(env) / "inbox").exists()


@pytest.mark.parametrize("name", ["alice", "Claudette", "Aiko", "Bob Smith"])
def test_a_person_s_name_can_answer(env, tmp_path, monkeypatch, name):
    _, found = put(env, tmp_path)
    at_terminal(monkeypatch)
    assert answer(env, found["request"]["id"], "延ばさない", name)[0] == 0


def test_an_automated_run_cannot_answer(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    at_terminal(monkeypatch)
    automated = {**env, "AI_HARNESS_AUTOMATED": "1"}
    assert answer(automated, found["request"]["id"])[0] == 3


def test_an_unknown_request_cannot_be_answered(env, monkeypatch):
    at_terminal(monkeypatch)
    assert answer(env, "DR-000000000000")[0] == 2
    assert run(env, "answer", "--request", "../x", "--option", "a", "--responder", "a")[0] == 2


def test_reuse_beyond_the_task_needs_an_end_in_the_future(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    request_id = found["request"]["id"]
    at_terminal(monkeypatch)
    assert answer(env, request_id, "延ばす", "alice", "--reuse-scope", "project")[0] == 2
    past = ["--reuse-scope", "project", "--reuse-until", "2026-01-01T00:00:00Z"]
    assert answer(env, request_id, "延ばす", "alice", *past)[0] == 2
    future = ["--reuse-scope", "project", "--reuse-until", "2026-12-31T00:00:00Z"]
    code, response, _ = answer(env, request_id, "延ばす", "alice", *future)
    assert code == 0
    assert response["reuse"] == {"scope": "project", "until": "2026-12-31T00:00:00Z"}


# poll --------------------------------------------------------------------------------------


def answered(env, tmp_path, monkeypatch, *extra, **task):
    _, found = put(env, tmp_path, **task)
    at_terminal(monkeypatch)
    code, response, _ = answer(env, found["request"]["id"], "延ばさない", "alice", *extra)
    assert code == 0
    return found["request"]["id"], response


def test_poll_returns_an_answer_once_then_says_it_was_delivered(env, tmp_path, monkeypatch):
    request_id, response = answered(env, tmp_path, monkeypatch)
    code, first = poll(env, "ws-a", "--head-sha", HEAD)
    assert code == 0 and first["status"] == "ok"
    assert first["responses"] == [{"request_id": request_id, "response": response}]
    assert [r["status"] for r in first["requests"]] == ["answered"]
    record = decisions_dir(env) / "delivered" / request_id / "ws-a.json"
    assert json.loads(record.read_text())["delivered_at"] == NOW
    code, second = poll(env, "ws-a", "--head-sha", HEAD)
    assert code == 0 and second["responses"] == []
    assert second["already_delivered"] == [
        {"request_id": request_id, "delivered_at": NOW, "response": response}
    ]


def test_each_workspace_that_put_the_request_gets_the_answer_once(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    put(env, tmp_path, workspace="ws-b", principal="workspace-b")
    poll(env, "ws-a", "--head-sha", HEAD)
    _, other = poll(env, "ws-b", "--head-sha", HEAD)
    assert [row["request_id"] for row in other["responses"]] == [request_id]


def test_a_workspace_sees_no_request_of_another(env, tmp_path, monkeypatch):
    answered(env, tmp_path, monkeypatch)
    code, seen = poll(env, "ws-z", "--head-sha", HEAD)
    assert code == 0 and seen["requests"] == [] and seen["responses"] == []


def test_poll_can_be_narrowed_to_a_task(env, tmp_path):
    put(env, tmp_path)
    _, other = put(env, tmp_path, task="TASK-0002")
    _, seen = poll(env, "ws-a", "--task", "TASK-0002")
    assert [r["id"] for r in seen["requests"]] == [other["request"]["id"]]


def test_an_answer_whose_evidence_changed_is_stale_and_not_applied(
    env, tmp_path, monkeypatch, clock
):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    changed = items(summary="R1: キューのメッセージ保持期間は 30 日にする")
    assert update(env, write_input(tmp_path / "in2.json", changed))[0] == 0
    code, seen = poll(env, "ws-a", "--head-sha", HEAD)
    assert code == 1 and seen["status"] == "stale_response"
    assert seen["stale"] == [{"request_id": request_id, "reasons": ["content_hash_changed"]}]
    assert seen["responses"] == [] and seen["already_delivered"] == []
    assert not (decisions_dir(env) / "delivered").exists()


def test_an_answer_for_another_head_is_stale(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    code, seen = poll(env, "ws-a", "--head-sha", OTHER_HEAD)
    assert code == 1 and seen["stale"] == [
        {"request_id": request_id, "reasons": ["head_sha_changed"]}
    ]
    code, seen = poll(env, "ws-a")
    assert code == 1 and seen["stale"][0]["reasons"] == ["head_sha_not_given"]


def test_an_answer_whose_evidence_left_the_index_is_stale(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    assert update(env, write_input(tmp_path / "in2.json", items()[1:]))[0] == 0
    code, seen = poll(env, "ws-a", "--head-sha", HEAD)
    assert code == 1
    assert seen["stale"] == [{"request_id": request_id, "reasons": ["evidence_unavailable"]}]


def test_asking_again_on_the_new_state_supersedes_the_stale_request(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    changed = items(summary="R1: キューのメッセージ保持期間は 30 日にする")
    update(env, write_input(tmp_path / "in2.json", changed))
    code, again = put(env, tmp_path)
    assert code == 0 and again["status"] == "accepted"
    code, seen = poll(env, "ws-a", "--head-sha", HEAD)
    assert code == 0 and seen["stale"] == []
    status = {r["id"]: r["status"] for r in seen["requests"]}
    assert status == {request_id: "superseded", again["request"]["id"]: "open"}


def test_an_answer_reused_beyond_the_task_is_only_a_hint(env, tmp_path, monkeypatch):
    root = state_root(env)
    index = root / "knowledge" / "index.sqlite3"
    before = index.read_bytes()
    future = ["--reuse-scope", "project", "--reuse-until", "2026-12-31T00:00:00Z"]
    request_id, _ = answered(env, tmp_path, monkeypatch, *future)
    _, seen = poll(env, "ws-a", "--head-sha", HEAD)
    assert seen["candidate_hints"] == [
        {
            "request_id": request_id,
            "question": QUESTION,
            "option": "延ばさない",
            "evidence_ids": [R1],
            "reuse": {"scope": "project", "until": "2026-12-31T00:00:00Z"},
        }
    ]
    assert index.read_bytes() == before
    assert not (root / "registry").exists()


def test_an_answer_for_the_task_only_gives_no_hint(env, tmp_path, monkeypatch):
    answered(env, tmp_path, monkeypatch)
    _, seen = poll(env, "ws-a", "--head-sha", HEAD)
    assert seen["candidate_hints"] == []


# expiry, cancel, list ----------------------------------------------------------------------


def test_a_request_past_its_end_is_expired_and_takes_no_answer(env, tmp_path, monkeypatch, clock):
    _, found = put(env, tmp_path, "--expires-at", "2026-11-28T00:00:00Z")
    request_id = found["request"]["id"]
    assert found["request"]["expires_at"] == "2026-11-28T00:00:00Z"
    clock["now"] = LATER
    _, seen = poll(env, "ws-a")
    assert [r["status"] for r in seen["requests"]] == ["expired"]
    at_terminal(monkeypatch)
    assert answer(env, request_id)[0] == 1
    code, again = put(env, tmp_path)
    assert code == 0 and again["status"] == "accepted" and again["request"]["id"] != request_id


def test_an_end_already_past_is_an_input_error(env, tmp_path):
    assert put(env, tmp_path, "--expires-at", "2026-01-01T00:00:00Z") == (2, None)
    assert put(env, tmp_path, "--expires-at", "tomorrow") == (2, None)


def test_a_cancelled_request_takes_no_answer_and_can_be_asked_again(env, tmp_path, monkeypatch):
    _, found = put(env, tmp_path)
    request_id = found["request"]["id"]
    code, value, _ = run(env, "cancel", "--request", request_id, "--reason", "質問を取り下げる")
    assert code == 0 and value["request"]["status"] == "cancelled"
    assert run(env, "cancel", "--request", request_id, "--reason", "二度目")[0] == 1
    at_terminal(monkeypatch)
    assert answer(env, request_id)[0] == 1
    code, again = put(env, tmp_path)
    assert again["status"] == "accepted" and again["request"]["id"] != request_id


def test_an_answered_or_unknown_request_cannot_be_cancelled(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    assert run(env, "cancel", "--request", request_id, "--reason", "遅い")[0] == 1
    assert run(env, "cancel", "--request", "DR-000000000000", "--reason", "無い")[0] == 2
    secret = "ghp_" + "a" * 36
    assert run(env, "cancel", "--request", request_id, "--reason", secret)[0] == 2


def test_list_shows_every_request_with_its_status_now(env, tmp_path, monkeypatch):
    request_id, _ = answered(env, tmp_path, monkeypatch)
    _, other = put(env, tmp_path, task="TASK-0002")
    code, listed, _ = run(env, "list")
    assert code == 0
    assert records.violations("decision-request", listed, "list_result") == []
    status = {r["id"]: r["status"] for r in listed["requests"]}
    assert status == {request_id: "answered", other["request"]["id"]: "open"}
    _, only, _ = run(env, "list", "--status", "open")
    assert [r["id"] for r in only["requests"]] == [other["request"]["id"]]


def test_list_with_no_queue_is_empty(env):
    code, listed, _ = run(env, "list")
    assert code == 0 and listed["requests"] == []


def test_a_damaged_request_file_stops_the_reader(env, tmp_path):
    _, found = put(env, tmp_path)
    path = decisions_dir(env) / "outbox" / f"{found['request']['id']}.json"
    os.chmod(path, 0o600)
    path.write_text("{")
    assert run(env, "list")[0] == 2
