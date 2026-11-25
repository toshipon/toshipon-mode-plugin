"""`review analyze`: review threads to lessons (`lessons.jsonl`, schema `review-lesson`).

Order of work:

1. Link every inline thread to its fix (`fix.attach`) and write `threads-with-fix.jsonl`.
2. Send the threads, the PR bodies and the patches of the paths they touch to the backend as
   DATA, a batch of whole PRs at a time. The backend proposes lesson text, the threads behind
   each lesson, exact quotes, a scope, and "this thread reverses that lesson" candidates.
3. Keep a proposed lesson only if every thread id exists in the batch and every quote is a
   substring of a comment in those threads. Others go to `rejected-lessons.jsonl` with reasons.
4. Compute the rest by fixed rules, never from the backend: `confidence` from the counts in
   `confidence_basis`, `source_trust` from who raised the point, `reviewer_agreement` from
   replies and resolution, and `staleness` from the opposition candidates whose timestamps show
   the opposing thread came later.

PR bodies and comments are external input: they travel as DATA and are never instructions.
"""

import re

from .. import paths, records
from ..errors import InputError, QualityGateError
from . import fix
from . import split as split_stage

STAGE = "analyze"
OUTPUT_SCHEMA = "llm-review-analysis"
MAX_BATCH_BYTES = 400 * 1024
MEMBER_ASSOCIATIONS = {"owner", "member", "collaborator"}

# The one place the confidence rule lives. First row whose bounds all hold wins.
CONFIDENCE_RULES = (
    (
        "strong",
        {"threads": 3, "distinct_prs": 3, "distinct_reviewers": 3},
        {"contradicting_threads": 0},
    ),
    ("recurring", {"threads": 3, "distinct_prs": 2, "distinct_reviewers": 2}, {}),
    ("candidate", {}, {}),
)

INSTRUCTIONS = """\
Task: extract reusable review lessons from the GitHub pull request review threads in DATA.

A lesson is a point a reviewer raised that could apply to later changes. Group threads that make
the same point into one lesson. For each lesson give:
- key: L1, L2, ... unique in your answer.
- thread_ids: ids of the threads in DATA that make the point. Use only ids that appear in DATA.
- target: the code or area the point is about.
- problem: what is wrong. rationale: why it matters.
- fix_summary: how it was fixed, or "" when it was not.
- applies_when: conditions under which the point applies.
- exceptions: cases a participant said the point does not apply to.
- quotes: exact text copied from comments of those threads, with the comment's id. Quotes are
  checked character by character; a lesson with a quote that is not in its threads is dropped.
- scope: general (any codebase), repo_specific (this repository's conventions) or
  case_specific (only this one change).

In oppositions, list a lesson whose policy a thread in DATA reverses or replaces (for example a
thread saying "use B instead of A" against a lesson "use A"), with that thread's id. Do not
decide which came first; the harness orders them by timestamp.

Do not rate confidence, trust, agreement or approval; the harness computes those."""

_SENTENCE = re.compile(r"(.+?[.!?。])(?=\s|$)")
STUB_EXCEPTION_MARKERS = ("on purpose", "intentionally", "意図的")
STUB_CASE_MARKERS = ("only here", "この pr だけ")


def confidence(basis):
    for level, at_least, at_most in CONFIDENCE_RULES:
        if all(basis[key] >= value for key, value in at_least.items()) and all(
            basis[key] <= value for key, value in at_most.items()
        ):
            return level
    raise AssertionError("the last confidence rule matches everything")


def source_trust(comments):
    """From the comments that raised the point: a member-level person, only bots, or neither.

    A bot is never member-level, whatever association GitHub reports for it.
    """
    for comment in comments:
        if (
            comment.get("author_type") == "user"
            and comment.get("author_association") in MEMBER_ASSOCIATIONS
        ):
            return "member_supported"
    if comments and all(comment.get("author_type") == "bot" for comment in comments):
        return "bot_only"
    return "external_only"


def _thread_agreement(thread):
    resolved = (thread.get("thread") or {}).get("is_resolved") is True
    if resolved or (thread.get("fix") or {}).get("status") == "found":
        return "agreed"
    return "disputed" if len(thread["comments"]) > 1 else "unanswered"


def reviewer_agreement(threads):
    """Any thread answered but neither resolved nor fixed makes the lesson disputed."""
    states = {_thread_agreement(thread) for thread in threads}
    for level in ("disputed", "agreed"):
        if level in states:
            return level
    return "unanswered"


def _raised_at(thread):
    return thread["comments"][0].get("created_at") if thread["comments"] else None


# DATA -------------------------------------------------------------------------------------


def _comment_data(comment):
    keys = ("id", "author_login", "author_type", "author_association", "created_at")
    item = {key: comment[key] for key in keys if key in comment}
    for key in ("in_reply_to", "body"):
        if key in comment:
            item[key] = comment[key]
    return item


def _thread_data(thread):
    item = {"id": thread["id"], "pr": thread["pr"]["number"], "kind": thread["kind"]}
    detail = thread.get("thread") or {}
    for key in ("path", "line", "is_resolved", "is_outdated", "diff_hunk"):
        if key in detail:
            item[key] = detail[key]
    if "review" in thread:
        item["review_state"] = thread["review"]["state"]
    if "fix" in thread:
        item["fix"] = {
            key: thread["fix"][key] for key in ("status", "method") if key in thread["fix"]
        }
    item["comments"] = [_comment_data(comment) for comment in thread["comments"]]
    return item


def _pull_data(number, pull, threads):
    touched = sorted({t["thread"]["path"] for t in threads if t.get("thread", {}).get("path")})
    files = [
        {"path": path, "patch": pull["files"][path]} for path in touched if path in pull["files"]
    ]
    return {"number": number, "body": pull["body"], "files": files}


def batches(threads, contexts, repo):
    """Whole PRs per batch, in PR order, each under MAX_BATCH_BYTES. Nothing is cut."""
    by_pull = {}
    for thread in threads:
        by_pull.setdefault(thread["pr"]["number"], []).append(thread)
    current, size, out = None, 0, []
    for number in sorted(by_pull):
        pull = _pull_data(number, contexts.get(number, fix.NO_PULL), by_pull[number])
        items = [_thread_data(thread) for thread in by_pull[number]]
        cost = len(records.canonical({"p": pull, "t": items}).encode("utf-8"))
        if cost > MAX_BATCH_BYTES:
            raise QualityGateError(
                f"PR {number}'s threads and patches are {cost} bytes, over the {MAX_BATCH_BYTES}-"
                "byte batch cap; stopped instead of truncating them."
            )
        if current is None or size + cost > MAX_BATCH_BYTES:
            current, size = {"repository": repo, "pull_requests": [], "threads": []}, 0
            out.append(current)
        current["pull_requests"].append(pull)
        current["threads"].extend(items)
        size += cost
    return out


# Stub -------------------------------------------------------------------------------------


def _first_sentence(body):
    line = (body or "").strip().split("\n", 1)[0].strip()
    match = _SENTENCE.match(line)
    return match.group(1) if match else line


def stub_analysis(data):
    """Fixed rules, no model: inline threads grouped by the first sentence of their first comment.

    A reply containing "on purpose" / "intentionally" becomes an exception; the first other reply
    becomes the fix summary; "only here" makes the scope case_specific; a lesson whose sentence
    says "instead of X" opposes every other lesson whose sentence contains X.
    """
    groups = {}
    for thread in data["threads"]:
        if thread["kind"] != "inline_thread" or not thread["comments"]:
            continue
        root = thread["comments"][0]
        sentence = _first_sentence(root.get("body"))
        if len(sentence) < 8 or sentence.startswith("```"):
            continue
        key = " ".join(sentence.lower().split())
        group = groups.setdefault(
            key,
            {"sentence": sentence, "threads": [], "quotes": [], "exceptions": [], "paths": set()},
        )
        group["threads"].append(thread["id"])
        group["quotes"].append({"comment_id": root["id"], "text": sentence})
        if thread.get("path"):
            group["paths"].add(thread["path"])
        if any(marker in (root.get("body") or "").lower() for marker in STUB_CASE_MARKERS):
            group["case"] = True
        for reply in thread["comments"][1:]:
            text = _first_sentence(reply.get("body"))
            if not text:
                continue
            if any(marker in text.lower() for marker in STUB_EXCEPTION_MARKERS):
                if text not in group["exceptions"]:
                    group["exceptions"].append(text)
                    group["quotes"].append({"comment_id": reply["id"], "text": text})
            elif "fix" not in group and reply.get("author_login") != root.get("author_login"):
                group["fix"] = text
    lessons, names = [], {}
    for number, (key, group) in enumerate(groups.items(), start=1):
        names[key] = f"L{number}"
        lessons.append(
            {
                "key": names[key],
                "thread_ids": group["threads"],
                "target": ", ".join(sorted(group["paths"])) or "unspecified",
                "problem": group["sentence"],
                "rationale": "Stub: threads grouped by the first sentence of their first comment.",
                "fix_summary": group.get("fix", ""),
                "applies_when": [],
                "exceptions": group["exceptions"],
                "quotes": group["quotes"],
                "scope": "case_specific" if group.get("case") else "general",
            }
        )
    oppositions = []
    for key, group in groups.items():
        if "instead of " not in key:
            continue
        replaced = key.split("instead of ", 1)[1].rstrip(".!?。 ")
        for other in groups:
            if other != key and replaced and replaced in other:
                oppositions.append(
                    {
                        "lesson": names[other],
                        "opposed_by_thread_ids": group["threads"],
                        "basis": "Stub: this wording replaces it ('instead of').",
                    }
                )
    return {"lessons": lessons, "oppositions": oppositions}


# Checks -----------------------------------------------------------------------------------


def _problems(draft, known):
    reasons = []
    for thread_id in draft["thread_ids"]:
        if thread_id not in known:
            reasons.append(f"unknown_thread: {thread_id} was not among the threads sent")
    if reasons:
        return reasons
    comments = {c["id"]: c for t in draft["thread_ids"] for c in known[t]["comments"]}
    for index, quote in enumerate(draft["quotes"]):
        comment = comments.get(quote["comment_id"])
        if comment is None:
            reasons.append(
                f"comment_not_in_threads: quotes[{index}] names a comment outside the lesson's threads"
            )
        elif not quote["text"].strip() or quote["text"] not in (comment.get("body") or ""):
            reasons.append(
                f"quote_not_found: quotes[{index}] is not a substring of comment {comment['id']}"
            )
    return reasons


class _Batch:
    """One backend answer, checked against the threads that were sent with it."""

    def __init__(self, output, threads, call_id):
        self.known = {thread["id"]: thread for thread in threads}
        self.call_id = call_id
        self.accepted = {}  # key -> draft
        self.rejected = []
        for draft in output["lessons"]:
            reasons = _problems(draft, self.known)
            if draft["key"] in self.accepted:
                reasons.insert(0, "duplicate_key: the key was used by an earlier lesson")
            if reasons:
                self.rejected.append({"key": draft["key"], "reasons": reasons, "draft": draft})
            else:
                draft = {**draft, "thread_ids": sorted(set(draft["thread_ids"]))}
                self.accepted[draft["key"]] = draft
        self.opposing = {key: set() for key in self.accepted}
        self.later = {key: set() for key in self.accepted}
        self.unordered = {key: set() for key in self.accepted}
        self.kept, self.ignored = [], []
        for opposition in output["oppositions"]:
            self._opposition(opposition)

    def _opposition(self, opposition):
        key = opposition["lesson"]
        for thread_id in opposition["opposed_by_thread_ids"]:
            entry = {"lesson": key, "thread_id": thread_id, "call_id": self.call_id}
            draft = self.accepted.get(key)
            if draft is None:
                self.ignored.append({**entry, "reason": "unknown_lesson"})
                continue
            if thread_id not in self.known:
                self.ignored.append({**entry, "reason": "unknown_thread"})
                continue
            if thread_id in draft["thread_ids"]:
                self.ignored.append({**entry, "reason": "self_reference"})
                continue
            self.opposing[key].add(thread_id)
            later = _raised_at(self.known[thread_id])
            times = [_raised_at(self.known[t]) for t in draft["thread_ids"]]
            known = [time for time in times if time is not None]
            if later is None or len(known) < len(times):
                self.unordered[key].add(thread_id)
            elif later > max(known):
                self.later[key].add(thread_id)
                self.kept.append({**entry, "basis": opposition["basis"]})
            else:
                self.ignored.append({**entry, "reason": "not_later"})

    def staleness(self, key):
        if self.later[key]:
            reason = "a later thread reverses or replaces this policy"
            return {
                "status": "superseded",
                "reason": reason,
                "later_evidence": sorted(self.later[key]),
            }
        if self.unordered[key]:
            reason = "an opposing thread has no timestamp to order it against this lesson"
            return {"status": "possibly_outdated", "reason": reason, "later_evidence": []}
        return {"status": "current", "reason": "no later opposing thread", "later_evidence": []}

    def lesson(self, key, run_id, now, backend):
        draft = self.accepted[key]
        threads = [self.known[thread_id] for thread_id in draft["thread_ids"]]
        roots = [thread["comments"][0] for thread in threads]
        reviewers = {
            root["author_login"]
            for root in roots
            if root.get("author_type") in {"user", "bot"} and root.get("author_login")
        }
        basis = {
            "threads": len(threads),
            "distinct_reviewers": len(reviewers),
            "distinct_prs": len({thread["pr"]["number"] for thread in threads}),
            "contradicting_threads": len(self.opposing[key]),
        }
        fields = ("target", "problem", "rationale", "fix_summary", "applies_when", "exceptions")
        return {
            "schema_version": records.SCHEMA_VERSION,
            "id": "",
            "run_id": run_id,
            "created_at": now,
            "thread_ids": draft["thread_ids"],
            "prs": sorted({thread["pr"]["number"] for thread in threads}),
            **{name: draft[name] for name in fields},
            "quotes": draft["quotes"],
            "confidence": confidence(basis),
            "confidence_basis": basis,
            "scope": draft["scope"],
            "source_trust": source_trust(roots),
            "staleness": self.staleness(key),
            "reviewer_agreement": reviewer_agreement(threads),
            "generated_by": {"backend": backend, "call_id": self.call_id},
        }


def _read_threads(run_dir):
    threads = records.read_jsonl(paths.child(run_dir, "threads.jsonl"))
    for thread in threads:
        problems = records.violations("review-thread", thread)
        if problems:
            raise InputError(
                f"threads.jsonl has an invalid record {thread.get('id')!r}: {problems[0]}"
            )
    return threads


def analyze(run_dir, session, split=None):
    """Run the stage. Returns the summary; raises QualityGateError when every lesson failed.

    With a split (`review split`), only generation-side events before its t1 are used.
    """
    run_id, now = run_dir.name, records.iso()
    threads = _read_threads(run_dir)
    contexts = fix.pull_contexts(run_dir)
    left_out = None
    if split is not None:
        threads, contexts, left_out = split_stage.restrict(threads, contexts, split)
    linked = fix.attach(run_dir, threads, contexts)
    records.write_jsonl(paths.child(run_dir, "threads-with-fix.jsonl"), linked)
    fixes = {status: 0 for status in ("found", "not_found", "ambiguous")}
    for thread in linked:
        if "fix" in thread:
            fixes[thread["fix"]["status"]] += 1

    repo = threads[0]["repo"] if threads else None
    lessons, rejected, kept, ignored, call_ids, drafts = [], [], [], [], [], 0
    for data in batches(linked, contexts, repo):
        output = session.ask(STAGE, INSTRUCTIONS, data, OUTPUT_SCHEMA, stub_analysis)
        call_ids.append(session.last_call_id)
        by_id = {thread["id"]: thread for thread in linked}
        batch = _Batch(
            output, [by_id[item["id"]] for item in data["threads"]], session.last_call_id
        )
        drafts += len(output["lessons"])
        for entry in batch.rejected:
            rejected.append({"call_id": batch.call_id, **entry})
        for key in batch.accepted:
            lesson = batch.lesson(key, run_id, now, session.backend)
            lesson["id"] = records.record_id("LS", len(lessons) + 1)
            problems = records.violations("review-lesson", lesson)
            if problems:
                reasons = [f"schema: {problem}" for problem in problems[:5]]
                rejected.append({"call_id": batch.call_id, "key": key, "reasons": reasons})
                continue
            lessons.append(lesson)
            kept += [
                {**entry, "lesson_id": lesson["id"]}
                for entry in batch.kept
                if entry["lesson"] == key
            ]
        ignored += batch.ignored

    rejected = [
        {"schema_version": records.SCHEMA_VERSION, "run_id": run_id, "created_at": now, **row}
        for row in rejected
    ]
    records.write_jsonl(paths.child(run_dir, "lessons.jsonl"), lessons)
    records.write_jsonl(paths.child(run_dir, "rejected-lessons.jsonl"), rejected)
    summary = {
        "run_id": run_id,
        "backend": session.backend,
        "threads": len(threads),
        "fix": fixes,
        "batches": len(call_ids),
        "drafts": drafts,
        "lessons": len(lessons),
        "rejected": len(rejected),
    }
    detail = {
        **summary,
        "schema_version": records.SCHEMA_VERSION,
        "created_at": now,
        "calls": call_ids,
        "oppositions": kept,
        "ignored_oppositions": ignored,
        "split_sha256": None if split is None else split["sha256"],
        "split_left_out": left_out,
    }
    records.write_json(paths.child(run_dir, "analyze.json"), detail)
    if drafts and not lessons:
        raise QualityGateError(
            f"all {drafts} proposed lessons failed the checks; reasons are in rejected-lessons.jsonl."
        )
    return summary
