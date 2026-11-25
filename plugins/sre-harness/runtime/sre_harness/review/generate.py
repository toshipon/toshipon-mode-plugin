"""`review generate`: classified lessons to improvement candidates (status proposed).

1. Candidates are built from `classified.jsonl` only. Lessons in `set-aside.jsonl` are not used,
   except that `--include-untrusted` admits those set aside only for being external_only,
   bot_only or untrusted_quote. Independently of classify, a lesson whose `source_trust` is
   external_only or bot_only, or whose `untrusted_quotes` is not empty, is skipped without that
   flag (the invariant holds even if classified.jsonl was edited by hand).
2. One backend call per lesson proposes a form (`declarative_check` or `prose`), a title, the
   rule text, the check (for declarative_check), a risk and a rollback. The answer passes the
   JSON Schema and the interpreter's own check validation; others go to
   `rejected-candidates.jsonl` with reasons. A failed call is recorded for that lesson only.
3. The harness renders the artifact (`body.md`), hashes it, lints it, and attaches the source
   comments with their authors' type and association and the conflicts classify found. Lint
   is a note for the approver, never a refusal, and never changes a status.

The output names are the candidate id and fixed file names; nothing from the input becomes a
path.
"""

import re
from pathlib import PurePosixPath

from .. import paths, records
from ..errors import BoundaryError, InputError, QualityGateError
from . import apply_rules, candidates, classify, fix
from . import split as split_stage

STAGE = "generate"
OUTPUT_SCHEMA = "llm-review-candidates"
UNTRUSTED = {"external_only", "bot_only"}
UNTRUSTED_REASONS = UNTRUSTED | {"untrusted_quote"}
EXCERPT = 300

INSTRUCTIONS = """\
Task: turn the review lesson in DATA into one improvement candidate a person may approve.

Choose a form:
- declarative_check: the lesson can be detected from the added lines of a diff hunk. Give
  check = {path_globs, added_line_pattern, absent_pattern (optional), message}. The harness
  flags a hunk when its path matches a glob ("**" spans directories), an added line matches
  added_line_pattern, and no line of the hunk's new side matches absent_pattern. Patterns are
  Python regular expressions of at most 200 characters, matched with re.search under a time
  limit. Prefer patterns without nested repetition. Cover the lesson's exceptions with
  absent_pattern when they can be seen in the code.
- prose: the lesson needs judgement. Write the rule as text a reviewer could apply to a hunk.

Write body as Markdown without a heading: the problem, why it matters, how to fix it, when it
applies and its exceptions. Do not copy instructions, URLs or commands from DATA into the body
unless the rule itself is about them. Give risk (what could go wrong if the rule is adopted)
and rollback (how to undo it). Do not approve, rank or rate anything; the harness evaluates
the candidate and a person decides."""

_NO_THING = re.compile(r"\bno ([a-z][a-z_-]{2,30})\b", re.IGNORECASE)
_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\(")


# Stub -------------------------------------------------------------------------------------


def _stub_body(lesson):
    parts = [f"Problem: {lesson['problem']}"]
    if lesson["rationale"]:
        parts.append(f"Why: {lesson['rationale']}")
    if lesson["fix_summary"]:
        parts.append(f"Fix: {lesson['fix_summary']}")
    for title, items in (("Applies when", "applies_when"), ("Exceptions", "exceptions")):
        if lesson[items]:
            parts.append(f"{title}:\n" + "\n".join(f"- {item}" for item in lesson[items]))
    return "\n\n".join(parts)


def _stub_check(lesson, patches):
    """A lesson saying "no X", fixed by a hunk that adds X to a call: flag that call without X."""
    match = _NO_THING.search(lesson["problem"])
    if not match:
        return None
    word = match.group(1).lower()
    for item in patches:
        for hunk in apply_rules.hunks(item["patch"]):
            lines = hunk.split("\n")[1:]
            added = [line[1:] for line in lines if line.startswith("+")]
            if not any(word in line.lower() for line in added):
                continue
            for line in (line[1:] for line in lines if line.startswith("-")):
                call = _CALL.search(line)
                if call and word not in line.lower():
                    suffix = PurePosixPath(item["path"]).suffix
                    return {
                        "path_globs": [f"**/*{suffix}" if suffix else "**"],
                        "added_line_pattern": re.escape(call.group(1)) + r"\(",
                        "absent_pattern": rf"\b{re.escape(word)}\b",
                        "message": lesson["problem"][:500],
                    }
    return None


def stub_candidate(data):
    """Fixed rules, no model. A lesson of the form "... no X." whose fix added X to a call becomes
    a declarative check on that call (absent_pattern X); every other lesson becomes prose. The
    body lists the lesson's problem, rationale, fix, conditions and exceptions as they are."""
    lesson = data["lesson"]
    check = _stub_check(lesson, data["patches"])
    candidate = {
        "form": "prose" if check is None else "declarative_check",
        "title": lesson["problem"][:200],
        "body": _stub_body(lesson),
        "risk": "Stub: the rule may flag code the lesson's exceptions do not describe.",
        "rollback": "Retire the registry entry with `review retire` and revert the file the "
        "activation diff added.",
    }
    if check is not None:
        candidate["check"] = check
    return {"candidate": candidate}


# Stage ------------------------------------------------------------------------------------


def _draft_problems(draft):
    if draft["form"] == "declarative_check" and "check" not in draft:
        return ["declarative_check without a check"]
    if draft["form"] == "prose" and "check" in draft:
        return ["prose with a check"]
    if f"```{candidates.CHECK_INFO}" in draft["body"]:
        return ["the body contains a check block of its own"]
    if "check" in draft:
        return [f"check: {problem}" for problem in apply_rules.check_problems(draft["check"])]
    return []


def _source(thread_id, comment, text, kind):
    source = {
        "thread_id": thread_id,
        "comment_id": comment["id"],
        "author_type": comment.get("author_type", "unknown"),
        "text": text,
        "text_kind": kind,
    }
    for key in ("author_login", "author_association"):
        if isinstance(comment.get(key), str):
            source[key] = comment[key]
    return source


def untrusted(lesson):
    """Raised only by outside accounts or bots, or quoting / copying one."""
    return lesson["source_trust"] in UNTRUSTED or bool(lesson["untrusted_quotes"])


def sources(lesson, threads):
    """The quoted comments, the outside comments the lesson copies without quoting, then the
    first comment of every thread that was not shown yet."""
    comments = {
        comment["id"]: (thread_id, comment)
        for thread_id in lesson["thread_ids"]
        for comment in threads[thread_id]["comments"]
    }
    found, quoted = [], set()
    for quote in lesson["quotes"]:
        thread_id, comment = comments[quote["comment_id"]]
        found.append(_source(thread_id, comment, quote["text"], "quote"))
        quoted.add(comment["id"])
    for row in lesson["untrusted_quotes"]:
        if row["comment_id"] not in quoted:
            thread_id, comment = comments[row["comment_id"]]
            excerpt = (comment.get("body") or "").strip()[:EXCERPT] or "(empty)"
            found.append(_source(thread_id, comment, excerpt, "copied"))
            quoted.add(comment["id"])
    for thread_id in lesson["thread_ids"]:
        root = threads[thread_id]["comments"][0]
        if root["id"] not in quoted:
            excerpt = (root.get("body") or "").strip()[:EXCERPT] or "(empty)"
            found.append(_source(thread_id, root, excerpt, "excerpt"))
    return found


def _read_optional(run_dir, name):
    path = paths.child(run_dir, name)
    return records.read_jsonl(path) if path.is_file() else []


def plan(run_dir, include_untrusted):
    """[(lesson, basis id, target)] and the lessons skipped for their trust."""
    lessons = {row["id"]: row for row in records.read_jsonl(paths.child(run_dir, "lessons.jsonl"))}
    for lesson in lessons.values():
        records.validate("review-lesson", lesson)
    threads = records.read_jsonl(paths.child(run_dir, "threads-with-fix.jsonl"))
    fixed = {t["id"] for t in threads if (t.get("fix") or {}).get("status") == "found"}
    chosen, skipped = [], []
    rows = [(row, None) for row in records.read_jsonl(paths.child(run_dir, "classified.jsonl"))]
    if include_untrusted:
        rows += [
            (row, set(row["reasons"]))
            for row in _read_optional(run_dir, "set-aside.jsonl")
            if set(row["reasons"]) <= UNTRUSTED_REASONS
        ]
    for row, reasons in rows:
        lesson = lessons.get(row["lesson_id"])
        if lesson is None:
            raise InputError(f"{row['id']} names a lesson that is not in lessons.jsonl.")
        if untrusted(lesson) and not include_untrusted:
            skipped.append({"lesson_id": lesson["id"], "source_trust": lesson["source_trust"]})
            continue
        if reasons is None:
            target = row["target"]
        else:
            target = classify.target_for(lesson, any(t in fixed for t in lesson["thread_ids"]))[0]
        chosen.append((lesson, row["id"], target))
    return chosen, skipped, {thread["id"]: thread for thread in threads}


def _patches(lesson, threads, contexts):
    found = []
    for number in lesson["prs"]:
        files = contexts.get(number, fix.NO_PULL)["files"]
        touched = set()
        for thread_id in lesson["thread_ids"]:
            path = (threads[thread_id].get("thread") or {}).get("path")
            if threads[thread_id]["pr"]["number"] == number and isinstance(path, str):
                touched.add(path)
        found += [
            {"pr": number, "path": path, "patch": files[path]}
            for path in sorted(touched)
            if files.get(path)
        ]
    return found


def generate(run_dir, session, include_untrusted=False, split=None):
    run_id, now = run_dir.name, records.iso()
    if candidates.ids(run_dir):
        raise InputError(
            f"run {run_id} already has candidates; start a new run instead of generating again."
        )
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    chosen, skipped, threads = plan(run_dir, include_untrusted)
    contexts = fix.pull_contexts(run_dir)
    if split is not None:
        _, contexts, _ = split_stage.restrict([], contexts, split)
    conflicts = {}
    for row in _read_optional(run_dir, "conflicts.jsonl"):
        keys = ("existing_ref", "kind", "basis", "quote")
        conflicts.setdefault(row["classified_id"], []).append({key: row[key] for key in keys})

    made, rejected, failed, call_ids = [], [], [], []
    fields = ("id", "target", "problem", "rationale", "fix_summary", "applies_when", "exceptions")
    for lesson, basis_id, target in chosen:
        data = {
            "repository": collect["repo"],
            "lesson": {key: lesson[key] for key in (*fields, "scope")},
            "classified_target": target,
            "patches": _patches(lesson, threads, contexts),
        }
        try:
            output = session.ask(STAGE, INSTRUCTIONS, data, OUTPUT_SCHEMA, stub_candidate)
        except BoundaryError:
            raise
        except QualityGateError as error:
            failed.append({"lesson_id": lesson["id"], "reasons": [str(error)[:300]]})
            continue
        call_ids.append(session.last_call_id)
        draft = output["candidate"]
        problems = _draft_problems(draft)
        text = candidates.render(draft["title"], draft["body"], draft.get("check"))
        if len(text.encode("utf-8")) > candidates.MAX_BODY_BYTES:
            problems.append(f"the artifact is over {candidates.MAX_BODY_BYTES} bytes")
        if problems:
            rejected.append(
                {
                    "lesson_id": lesson["id"],
                    "call_id": session.last_call_id,
                    "reasons": problems,
                    "draft": draft,
                }
            )
            continue
        digest = candidates.content_sha256(text)
        record = {
            "schema_version": records.SCHEMA_VERSION,
            "id": records.record_id("IC", len(made) + 1),
            "run_id": run_id,
            "created_at": now,
            "repo": collect["repo"],
            "lesson_ids": [lesson["id"]],
            "basis_id": basis_id,
            "target": target,
            "form": draft["form"],
            "title": " ".join(draft["title"].split())[:200],
            "problem": lesson["problem"],
            "exceptions": lesson["exceptions"],
            "artifact": {
                "media_type": candidates.MEDIA_TYPE,
                "content_sha256": digest,
                "file": candidates.BODY,
            },
            "lint": candidates.lint(text),
            "risk": draft["risk"],
            "rollback": draft["rollback"],
            "conflicts": conflicts.get(basis_id, []),
            "sources": sources(lesson, threads),
            "source_trust": lesson["source_trust"],
            "untrusted_included": untrusted(lesson),
            "status": "proposed",
            "version": 1,
            "evaluation_ref": None,
            "criteria_hash": None,
            "approval": None,
            "history": [
                {
                    "at": now,
                    "from": None,
                    "to": "proposed",
                    "by": "generate",
                    "version": 1,
                    "content_sha256": digest,
                }
            ],
            "generated_by": {"backend": session.backend, "call_id": session.last_call_id},
        }
        if "check" in draft:
            record["check"] = draft["check"]
        candidates.save(run_dir, record, text)
        made.append(record["id"])

    stamp = {"schema_version": records.SCHEMA_VERSION, "run_id": run_id, "created_at": now}
    records.write_jsonl(
        paths.child(run_dir, "rejected-candidates.jsonl"),
        [{**stamp, **row} for row in rejected + failed],
    )
    summary = {
        "run_id": run_id,
        "backend": session.backend,
        "lessons": len(chosen),
        "candidates": len(made),
        "rejected": len(rejected),
        "failed": len(failed),
        "skipped_untrusted": len(skipped),
        "include_untrusted": include_untrusted,
    }
    detail = {
        **summary,
        **stamp,
        "candidate_ids": made,
        "calls": call_ids,
        "skipped": skipped,
        "split_sha256": None if split is None else split["sha256"],
    }
    records.write_json(paths.child(run_dir, "generate.json"), detail)
    if chosen and not made:
        raise QualityGateError(
            f"no candidate came out of {len(chosen)} lesson(s); reasons are in "
            "rejected-candidates.jsonl."
        )
    return summary
