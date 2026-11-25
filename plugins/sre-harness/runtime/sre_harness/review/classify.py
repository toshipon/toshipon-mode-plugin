"""`review classify`: decide what each lesson could become, and how it relates to existing rules.

1. Lessons that must not become candidates go to `set-aside.jsonl` with every reason that
   applies (`SET_ASIDE`): superseded, case-specific, or raised only by outside accounts or bots.
   They are kept for a person to read; later stages do not build candidates from them.
2. Every other lesson gets a target from `TARGET_RULES`, a fixed table: the first row whose
   pattern and condition hold. No model is involved (`classified.jsonl`).
3. The backend proposes relations between those lessons and the lines of the Markdown files
   under `--existing-rules` (contradicts / duplicates / narrows). A relation is kept only when
   the lesson is a candidate, the line exists and the quote is a substring of it
   (`conflicts.jsonl`); others go to `rejected-conflicts.jsonl`. Relations are information for
   the approver: they never change a target, a set-aside decision or the exit code.

Rule files are read, never written. Symlinks are not followed, and a file or a total over the
size caps stops the stage instead of being cut.
"""

import os
import re
from pathlib import Path

from .. import paths, records
from ..errors import BoundaryError, InputError

STAGE = "classify"
OUTPUT_SCHEMA = "llm-review-conflicts"
TARGETS = ("rule", "skill", "policy_as_code", "evaluation", "knowledge", "regression_test")
MAX_RULE_FILE_BYTES = 256 * 1024
MAX_RULES_BYTES = 512 * 1024
MAX_RULE_FILES = 500

SET_ASIDE = (
    ("superseded", lambda lesson: lesson["staleness"]["status"] == "superseded"),
    ("case_specific", lambda lesson: lesson["scope"] == "case_specific"),
    ("external_only", lambda lesson: lesson["source_trust"] == "external_only"),
    ("bot_only", lambda lesson: lesson["source_trust"] == "bot_only"),
)

# (rule name, target, pattern over target + problem + fix_summary + applies_when, condition).
# The first row whose pattern (if any) matches and whose condition (if any) holds decides.
TARGET_RULES = (
    (
        "iac_or_access_policy",
        "policy_as_code",
        r"\.tf\b|\.tfvars\b|terraform|cloudformation|\biam\b|security group|bucket polic"
        r"|kubernetes|\bk8s\b|helm chart|\.github/workflows",
        None,
    ),
    (
        "fixed_bug",
        "regression_test",
        r"\bbug\b|regress|crash|off-by-one|\bfails?\b|broken|race condition|バグ|不具合",
        "fixed",
    ),
    (
        "measurement",
        "evaluation",
        r"latency|throughput|benchmark|performance|\bp9[59]\b|性能|計測",
        None,
    ),
    ("procedure", "skill", r"how to|procedure|\bsteps?\b|runbook|checklist|手順", None),
    ("repeated_point", "rule", None, "repeated"),
    ("default", "knowledge", None, None),
)
_CONDITIONS = {
    None: lambda lesson, fixed: True,
    "fixed": lambda lesson, fixed: fixed,
    "repeated": lambda lesson, fixed: lesson["confidence"] in {"recurring", "strong"},
}

INSTRUCTIONS = """\
Task: compare each lesson in DATA with the existing rules in DATA and list the pairs a reviewer
should read together:
- contradicts: the rule allows what the lesson forbids, or forbids what it asks for.
- duplicates: the rule already says what the lesson says.
- narrows: the rule and the lesson cover the same point, but one adds a condition or an
  exception the other does not have.
For each pair give lesson_id and existing_ref exactly as in DATA, a one-sentence basis, and a
quote copied exactly from that rule's text (it is checked character by character). Return an
empty list when nothing qualifies. Do not rank, approve or rewrite anything."""

STUB_STOPWORDS = {"the", "this", "that", "has", "have", "for", "with", "and", "are", "its", "not"}
STUB_PERMISSIVE = ("may omit", "may skip", "need not", "not required", "optional", "不要")
_LIST_MARKER = re.compile(r"^(?:[-*+]|[0-9]+[.)])\s+")


def set_aside_reasons(lesson):
    return [reason for reason, applies in SET_ASIDE if applies(lesson)]


def target_for(lesson, fixed):
    """(target, rule name, basis) from the first matching row of TARGET_RULES."""
    text = " ".join(
        [lesson["target"], lesson["problem"], lesson["fix_summary"], *lesson["applies_when"]]
    ).lower()
    for name, target, pattern, condition in TARGET_RULES:
        match = re.search(pattern, text) if pattern else None
        if (pattern is None or match) and _CONDITIONS[condition](lesson, fixed):
            basis = f"rule {name}"
            if match:
                basis += f": matched {match.group(0)!r}"
            if condition:
                basis += f"; condition {condition}"
            return target, name, basis
    raise AssertionError("the default target rule matches everything")


# Existing rules ---------------------------------------------------------------------------


def _read(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(path, flags)
    except OSError as error:
        raise InputError(f"cannot read a rule file: {error.strerror}") from None
    with os.fdopen(handle, "rb") as stream:
        data = stream.read(MAX_RULE_FILE_BYTES + 1)
    if len(data) > MAX_RULE_FILE_BYTES:
        raise InputError(
            f"a rule file is over {MAX_RULE_FILE_BYTES} bytes; stopped instead of cutting it."
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise InputError("a rule file is not UTF-8 text.") from None


def _statements(name, text):
    """One statement per non-empty line outside code fences, headings and comments."""
    found, fenced = [], False
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced or not stripped or stripped.startswith(("#", "<!--")):
            continue
        statement = _LIST_MARKER.sub("", stripped)
        if statement:
            found.append({"ref": f"{name}#L{number}", "text": statement})
    return found


def load_rules(directory):
    """(statements, skipped symlinks) from the Markdown files under `directory`."""
    root = Path(directory)
    if root.is_symlink():
        raise BoundaryError(
            "refused --existing-rules: it is a symlink, and symlinks are not followed."
        )
    if not root.is_dir():
        raise InputError(f"--existing-rules is not a directory: {root}")
    statements, skipped, files, total = [], [], 0, 0
    for current, directories, names in os.walk(root, followlinks=False):
        here = Path(current)
        for name in list(directories):
            if (here / name).is_symlink():
                skipped.append((here / name).relative_to(root).as_posix())
        directories[:] = sorted(
            name
            for name in directories
            if not name.startswith(".") and not (here / name).is_symlink()
        )
        for name in sorted(names):
            path = here / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                skipped.append(relative)
                continue
            if not name.endswith(".md"):
                continue
            files += 1
            if files > MAX_RULE_FILES:
                raise InputError(
                    f"--existing-rules holds more than {MAX_RULE_FILES} Markdown files."
                )
            text = _read(path)
            total += len(text.encode("utf-8"))
            if total > MAX_RULES_BYTES:
                raise InputError(
                    f"the rule files pass {MAX_RULES_BYTES} bytes in total; stopped instead of "
                    "cutting them."
                )
            statements += _statements(relative, text)
    return statements, sorted(skipped)


# Stub -------------------------------------------------------------------------------------


def _tokens(text):
    found = set()
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        if len(word) < 3 or word in STUB_STOPWORDS:
            continue
        if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        found.add(word)
    return found


def stub_conflicts(data):
    """Fixed rules, no model: a rule line sharing at least three words and half of the lesson's
    problem is related. A permissive line ("may omit", "optional") contradicts; otherwise a
    lesson with exceptions narrows, and one without duplicates."""
    conflicts = []
    for lesson in data["lessons"]:
        words = _tokens(lesson["problem"])
        for rule in data["rules"]:
            shared = words & _tokens(rule["text"])
            if len(shared) < 3 or len(shared) * 2 < len(words):
                continue
            if any(marker in rule["text"].lower() for marker in STUB_PERMISSIVE):
                kind = "contradicts"
            else:
                kind = "narrows" if lesson["exceptions"] else "duplicates"
            conflicts.append(
                {
                    "lesson_id": lesson["id"],
                    "existing_ref": rule["ref"],
                    "kind": kind,
                    "basis": "Stub: shares " + ", ".join(sorted(shared)) + ".",
                    "quote": rule["text"],
                }
            )
    return {"conflicts": conflicts}


# Stage ------------------------------------------------------------------------------------


def _conflict_problems(item, candidates, rules):
    if item["lesson_id"] not in candidates:
        return [f"not_a_candidate: {item['lesson_id']} is not a classified lesson"]
    rule = rules.get(item["existing_ref"])
    if rule is None:
        return [f"unknown_rule: {item['existing_ref']!r} is not a line that was sent"]
    if not item["quote"].strip() or item["quote"] not in rule["text"]:
        return [f"quote_not_found: the quote is not a substring of {item['existing_ref']}"]
    return []


def _lessons(run_dir):
    lessons = records.read_jsonl(paths.child(run_dir, "lessons.jsonl"))
    for lesson in lessons:
        problems = records.violations("review-lesson", lesson)
        if problems:
            raise InputError(
                f"lessons.jsonl has an invalid record {lesson.get('id')!r}: {problems[0]}"
            )
    return lessons


def classify(run_dir, rules_dir, session):
    run_id, now = run_dir.name, records.iso()
    lessons = _lessons(run_dir)
    threads = records.read_jsonl(paths.child(run_dir, "threads-with-fix.jsonl"))
    fixed = {t["id"] for t in threads if (t.get("fix") or {}).get("status") == "found"}
    statements, skipped = load_rules(rules_dir)

    def stamp(prefix, number, **fields):
        return {
            "schema_version": records.SCHEMA_VERSION,
            "id": records.record_id(prefix, number),
            "run_id": run_id,
            "created_at": now,
            **fields,
        }

    classified, aside = [], []
    for lesson in lessons:
        reasons = set_aside_reasons(lesson)
        if reasons:
            aside.append(stamp("SA", len(aside) + 1, lesson_id=lesson["id"], reasons=reasons))
            continue
        target, rule, basis = target_for(lesson, any(t in fixed for t in lesson["thread_ids"]))
        row = stamp("CL", len(classified) + 1, lesson_id=lesson["id"], target=target, rule=rule)
        classified.append({**row, "basis": basis})

    candidates = {row["lesson_id"]: row["id"] for row in classified}
    conflicts, rejected = [], []
    if candidates and statements:
        fields = ("id", "target", "problem", "fix_summary", "applies_when", "exceptions")
        data = {
            "lessons": [
                {key: lesson[key] for key in fields}
                for lesson in lessons
                if lesson["id"] in candidates
            ],
            "rules": statements,
        }
        output = session.ask(STAGE, INSTRUCTIONS, data, OUTPUT_SCHEMA, stub_conflicts)
        detector = "stub" if session.backend == "stub" else "llm"
        rules = {statement["ref"]: statement for statement in statements}
        seen = set()
        for item in output["conflicts"]:
            problems = _conflict_problems(item, candidates, rules)
            if problems:
                rejected.append(
                    stamp(
                        "RC",
                        len(rejected) + 1,
                        reasons=problems,
                        conflict=item,
                        call_id=session.last_call_id,
                    )
                )
                continue
            key = (item["lesson_id"], item["existing_ref"], item["kind"])
            if key in seen:
                continue
            seen.add(key)
            conflicts.append(
                stamp(
                    "CF",
                    len(conflicts) + 1,
                    classified_id=candidates[item["lesson_id"]],
                    detector=detector,
                    **item,
                )
            )

    for name, rows, schema, definition in (
        ("classified.jsonl", classified, "review-classified", None),
        ("set-aside.jsonl", aside, "review-classified", "set_aside"),
        ("conflicts.jsonl", conflicts, "review-conflict", None),
    ):
        for row in rows:
            records.validate(schema, row, definition)
        records.write_jsonl(paths.child(run_dir, name), rows)
    records.write_jsonl(paths.child(run_dir, "rejected-conflicts.jsonl"), rejected)
    summary = {
        "run_id": run_id,
        "classified": len(classified),
        "set_aside": len(aside),
        "conflicts": len(conflicts),
        "rejected_conflicts": len(rejected),
        "rules": len(statements),
        "skipped_rule_files": skipped,
    }
    records.write_json(
        paths.child(run_dir, "classify.json"),
        {
            **summary,
            "schema_version": records.SCHEMA_VERSION,
            "created_at": now,
            "backend": session.backend,
        },
    )
    return summary
