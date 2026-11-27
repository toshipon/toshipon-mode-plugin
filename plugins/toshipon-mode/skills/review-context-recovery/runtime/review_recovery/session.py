"""One PR's recovery session as an append-only log. The current comment is the first thread
that is neither recapped nor skipped, and it moves through four phases in order:

    presented -> explained -> answered -> recapped

`next` hands out that one thread and nothing else until its recap is recorded, so the
explanation, the user's own answer and the recap cannot be skipped or batched."""

import os
from datetime import datetime

from .foundation import (
    PREAMBLE,
    Evidence,
    Refusal,
    append_private,
    envelope,
    gitread,
    now,
    one_line,
    parse_locator,
    project_dir,
    read_jsonl,
    redact,
    verify,
)

PHASES = ("new", "presented", "explained", "answered", "recapped", "skipped")
ASSESSMENTS = ("correct", "partial", "misconception")
EXPLANATION_TEXTS = ("concern", "failure_scenario", "blast_radius", "proposed_fix")
MAX_DIFF_LINES = 200
MAX_RECAP = 200
# Set by the improvement harness for scheduled runs and for the child processes it starts.
_NOT_A_PERSON = ("AI_HARNESS_AUTOMATED", "AI_HARNESS_ACTIVE")


def _login(comment):
    user = comment.get("user") or comment.get("author") or {}
    return one_line(str(user.get("login", "") if isinstance(user, dict) else user), 60) or "unknown"


def parse_threads(raw, only=None):
    """Threads from review comments as GitHub's REST API returns them
    (`gh api repos/{owner}/{repo}/pulls/{n}/comments`), or from {"comments": [...]}.
    A thread is a root comment and its replies, in the order they were written."""
    comments = raw.get("comments") if isinstance(raw, dict) else raw
    if not isinstance(comments, list) or not comments:
        raise Refusal("threads file must be a non-empty list of review comments")
    parsed = {}
    for comment in comments:
        if not isinstance(comment, dict) or not isinstance(comment.get("id"), int):
            raise Refusal("every review comment needs an integer id")
        if not isinstance(comment.get("body"), str):
            raise Refusal(f"comment {comment['id']} has no body")
        line = comment.get("line") or comment.get("original_line")
        parsed[comment["id"]] = {
            "id": comment["id"],
            "author": _login(comment),
            "path": comment.get("path") if isinstance(comment.get("path"), str) else None,
            "line": line if isinstance(line, int) else None,
            "body": comment["body"],
            "created_at": str(comment.get("created_at", "")),
            "in_reply_to": comment.get("in_reply_to_id"),
        }
    threads = {}
    for comment in sorted(parsed.values(), key=lambda c: (c["created_at"], c["id"])):
        root = comment
        seen = set()
        while root["in_reply_to"] in parsed and root["id"] not in seen:
            seen.add(root["id"])
            root = parsed[root["in_reply_to"]]
        threads.setdefault(root["id"], []).append(comment)
    ordered = sorted(
        threads, key=lambda root: (parsed[root]["path"] or "", parsed[root]["line"] or 0, root)
    )
    if only is not None:
        owner = next(
            (root for root in ordered if any(c["id"] == only for c in threads[root])), None
        )
        if owner is None:
            raise Refusal(f"comment {only} is not in the threads file")
        ordered = [owner]
    return [{"id": root, "comments": threads[root]} for root in ordered]


class Session:
    def __init__(self, root, pr):
        if not isinstance(pr, int) or pr < 1:
            raise Refusal(f"PR must be a positive number: {pr!r}")
        self.root = root
        self.pr = pr
        self.home = project_dir("review-context", root, f"pr-{pr}")
        self.log = self.home / "events.jsonl"
        self.evidence = Evidence(root, self.home / "evidence")

    # -- reading the log ---------------------------------------------------------------

    def state(self):
        events = read_jsonl(self.log)
        if not events:
            raise Refusal(f"no session for PR #{self.pr}; run start first")
        started = events[0]
        threads = {
            thread["id"]: {**thread, "phase": "new", "events": []} for thread in started["threads"]
        }
        for event in events[1:]:
            thread = threads[event["thread"]]
            thread["events"].append(event)
            thread["phase"] = event["phase"]
        return {"base": started["base"], "head": started["head"], "threads": list(threads.values())}

    def _current(self, state):
        return next(
            (t for t in state["threads"] if t["phase"] not in ("recapped", "skipped")), None
        )

    def _require(self, phases, what):
        state = self.state()
        thread = self._current(state)
        if thread is None:
            raise Refusal("every comment in this session is recapped or skipped")
        if thread["phase"] not in phases:
            raise Refusal(
                f"comment {thread['id']} is at '{thread['phase']}'; {what} comes after {' or '.join(phases)}",
                [f"owed: {owed(thread['phase'])}"],
            )
        return state, thread

    def _record(self, thread, event, phase, **fields):
        record = {
            "event": event,
            "at": now(),
            "thread": thread["id"],
            "phase": phase,
            **redact(fields),
        }
        append_private(self.log, record)
        return record

    # -- commands ----------------------------------------------------------------------

    def start(self, base, head, raw_threads, only=None):
        if self.log.exists():
            raise Refusal(
                f"a session for PR #{self.pr} already exists; continue with next ({self.log})"
            )
        revisions = {}
        for name, revision in (("base", base), ("head", head)):
            revisions[name] = gitread.resolve(self.root, revision)
            if revisions[name] is None:
                raise Refusal(
                    f"{name} revision is not in the repository (fetch it first): {revision}"
                )
        threads = parse_threads(raw_threads, only)
        for thread in threads:
            for comment in thread["comments"]:
                saved = self.evidence.add(
                    f"pr:{self.pr}#comment-{comment['id']}", revisions["head"], comment["body"]
                )
                comment["sha256"] = saved["sha256"]
                del comment["body"]  # the snapshot is the one copy; the log holds metadata
        append_private(
            self.log,
            {"event": "started", "at": now(), "pr": self.pr, **revisions, "threads": threads},
        )
        return {"pr": self.pr, **revisions, "threads": len(threads), "log": str(self.log)}

    def next(self):
        state = self.state()
        thread = self._current(state)
        if thread is None:
            return {"action": "done", "threads": len(state["threads"])}
        if thread["phase"] == "new":
            self._record(thread, "presented", "presented")
            thread["phase"] = "presented"
        return {
            "action": owed(thread["phase"]),
            "preamble": PREAMBLE,
            "pr": self.pr,
            "base": state["base"],
            "head": state["head"],
            "thread": self._present(thread, state),
            "remaining": sum(t["phase"] not in ("recapped", "skipped") for t in state["threads"])
            - 1,
        }

    def _present(self, thread, state):
        root = thread["comments"][0]
        comments = []
        for comment in thread["comments"]:
            locator = f"pr:{self.pr}#comment-{comment['id']}"
            body = self.evidence.resolve(locator, state["head"]) or ""
            comments.append(
                {
                    "locator": locator,
                    "revision": state["head"],
                    "author": comment["author"],
                    "path": comment["path"],
                    "line": comment["line"],
                    "body": envelope(f"{locator}@{state['head'][:12]}", body),
                }
            )
        presented = {"id": thread["id"], "phase": thread["phase"], "comments": comments}
        if root["path"]:
            try:
                diff = gitread.diff(self.root, state["base"], state["head"], root["path"])
            except Refusal:
                diff = None
            if diff:
                lines = diff.splitlines()
                presented["diff"] = envelope(
                    f"git diff {root['path']}", "\n".join(lines[:MAX_DIFF_LINES])
                )
                presented["diff_truncated"] = len(lines) > MAX_DIFF_LINES
        return presented

    def explain(self, explanation):
        state, thread = self._require(("presented", "explained"), "explain")
        problems = check_explanation(explanation)
        if problems:
            raise Refusal("explanation does not follow the contract", problems)
        report = verify(explanation["claims"], self.evidence.resolve)
        cited = {
            parse_locator(reference["locator"]).get("comment")
            for claim in explanation["claims"]
            for reference in claim.get("references", [])
        }
        if str(thread["id"]) not in cited:
            report["results"].append(
                {
                    "problems": [
                        f"no claim cites the comment itself (pr:{self.pr}#comment-{thread['id']})"
                    ]
                }
            )
        problems = [problem for result in report["results"] for problem in result["problems"]]
        if problems:
            self._record(thread, "explain_rejected", thread["phase"], problems=problems)
            raise Refusal("explanation cites evidence that does not read back", problems)
        self._record(
            thread, "explained", "explained", explanation=explanation, claims=report["by_status"]
        )
        return {
            "action": "ask",
            "thread": thread["id"],
            "check_question": explanation["check_question"],
        }

    def answer(self, text, assessment, correction=""):
        if any(os.environ.get(name) for name in _NOT_A_PERSON):
            raise Refusal(
                "the answer is the user's own; it is not recorded from a scheduled or child process"
            )
        _, thread = self._require(("explained",), "answer")
        if not text.strip():
            raise Refusal("the user's answer is empty")
        if assessment not in ASSESSMENTS:
            raise Refusal(f"assessment must be one of {', '.join(ASSESSMENTS)}")
        if assessment != "correct" and not correction.strip():
            raise Refusal(
                f"'{assessment}' needs --correction: what was off and what the evidence says"
            )
        self._record(
            thread,
            "answered",
            "answered",
            answer=text,
            assessment=assessment,
            correction=correction,
        )
        return {"action": "recap", "thread": thread["id"], "assessment": assessment}

    def recap(self, intent, risk, next_action):
        _, thread = self._require(("answered",), "recap")
        bullets = {"intent": intent, "risk": risk, "next_action": next_action}
        problems = [
            f"{name} must be one line of 1-{MAX_RECAP} characters"
            for name, text in bullets.items()
            if not text.strip() or "\n" in text.strip() or len(text.strip()) > MAX_RECAP
        ]
        if problems:
            raise Refusal("a recap is exactly three bullets: intent, risk, next action", problems)
        self._record(
            thread, "recapped", "recapped", recap={k: v.strip() for k, v in bullets.items()}
        )
        return {"action": "next", "thread": thread["id"]}

    def skip(self, reason):
        state = self.state()
        thread = self._current(state)
        if thread is None:
            raise Refusal("every comment in this session is recapped or skipped")
        if not reason.strip():
            raise Refusal("skip needs the reason the user gave")
        self._record(thread, "skipped", "skipped", reason=one_line(reason))
        return {"action": "next", "thread": thread["id"]}

    def status(self):
        state = self.state()
        threads = [thread_metrics(thread) for thread in state["threads"]]
        measured = [
            t["understanding_seconds"] for t in threads if t["understanding_seconds"] is not None
        ]
        return {
            "pr": self.pr,
            "base": state["base"],
            "head": state["head"],
            "threads": threads,
            "metrics": {
                "comments": len(threads),
                "recapped": sum(t["phase"] == "recapped" for t in threads),
                "understanding_seconds": measured,
                "median_understanding_seconds": sorted(measured)[len(measured) // 2]
                if measured
                else None,
                "misconceptions": sum(t["assessment"] == "misconception" for t in threads),
                "explanations_rejected": sum(t["explanations_rejected"] for t in threads),
            },
        }


def owed(phase):
    return {"new": "explain", "presented": "explain", "explained": "ask", "answered": "recap"}.get(
        phase, "next"
    )


def check_explanation(explanation):
    """Problems with the shape of an explanation. Evidence is checked separately."""
    if not isinstance(explanation, dict):
        return ["explanation must be an object"]

    def text(value):
        return isinstance(value, str) and bool(value.strip())

    problems = []
    intent = explanation.get("intent")
    if (
        not isinstance(intent, dict)
        or not text(intent.get("original"))
        or not text(intent.get("current"))
    ):
        problems.append(
            "intent needs both 'original' (what was meant) and 'current' (what the code does)"
        )
    problems += [f"{key} is empty" for key in EXPLANATION_TEXTS if not text(explanation.get(key))]
    alternatives = explanation.get("alternatives")
    if not isinstance(alternatives, list) or not alternatives:
        problems.append("alternatives needs at least one option other than the proposed fix")
    elif not all(
        isinstance(a, dict) and text(a.get("option")) and text(a.get("tradeoff"))
        for a in alternatives
    ):
        problems.append("each alternative needs 'option' and 'tradeoff'")
    question = explanation.get("check_question")
    if not isinstance(question, str) or not question.strip():
        problems.append("check_question is empty")
    elif (
        "\n" in question.strip()
        or len(question) > 300
        or sum(question.count(mark) for mark in "?？") > 1
    ):
        problems.append("check_question must be one question on one line (at most 300 characters)")
    if not isinstance(explanation.get("claims"), list) or not explanation["claims"]:
        problems.append("claims needs at least one claim")
    return problems


def thread_metrics(thread):
    """Seconds from the comment being presented to the user's first answer, when one exists."""
    at = {}
    for event in thread["events"]:
        at.setdefault(event["event"], event["at"])
    answered = next((e for e in thread["events"] if e["event"] == "answered"), None)
    seconds = None
    if answered and "presented" in at:
        elapsed = datetime.fromisoformat(answered["at"]) - datetime.fromisoformat(at["presented"])
        seconds = int(elapsed.total_seconds())
    return {
        "id": thread["id"],
        "phase": thread["phase"],
        "owed": owed(thread["phase"]),
        "understanding_seconds": seconds,
        "assessment": answered["assessment"] if answered else None,
        "explanations_rejected": sum(e["event"] == "explain_rejected" for e in thread["events"]),
    }
