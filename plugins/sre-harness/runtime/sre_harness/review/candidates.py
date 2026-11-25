"""Improvement candidates on disk: `candidates/<IC-id>/candidate.json` and the artifact `body.md`.

`body.md` is what a person reads, approves and later copies into a repository. Its SHA-256 is
`artifact.content_sha256`. A declarative check lives in the artifact as one fenced block
(`CHECK_INFO`), so the hash covers the check as well as the prose. `body.md` is the only source
of what is applied and recorded: `content` parses the title, form and check from it, and refuses
a record whose copies of them disagree. Everything else in candidate.json is context the hash
does not cover. A person may edit `body.md`: the next `review evaluate` sees the new hash, starts a
new version and evaluates that. `review approve` refuses a body whose hash differs from the
record.

Status changes go through `transition`, which knows the allowed moves (`TRANSITIONS`).
"""

import json
import re

from .. import paths, privacy, records
from ..errors import BoundaryError, InputError, QualityGateError
from . import apply_rules

DIRECTORY = "candidates"
RECORD = "candidate.json"
BODY = "body.md"
MEDIA_TYPE = "text/markdown"
CHECK_INFO = "json sre-harness-check"
MAX_BODY_BYTES = 64 * 1024
CANDIDATE_ID = re.compile(r"IC-[0-9]{4,}")

# Who may move a candidate from one status to another. A body change starts again at proposed.
TRANSITIONS = {
    ("proposed", "proposed"): "evaluate",
    ("proposed", "evaluated"): "evaluate",
    ("evaluated", "evaluated"): "evaluate",
    ("evaluated", "approved"): "approve",
    ("proposed", "rejected"): "reject",
    ("evaluated", "rejected"): "reject",
    ("approved", "retired"): "retire",
    ("evaluated", "proposed"): "evaluate",
    ("approved", "proposed"): "evaluate",
    ("rejected", "proposed"): "evaluate",
    ("retired", "proposed"): "evaluate",
}

LINT_RULES = (
    (
        "imperative_to_agent",
        r"(?i)\b(?:ai|agents?|assistants?|claude|llms?|chatbots?)\b[^.\n]{0,60}?"
        r"\b(?:ignore|disregard|forget|override|approve|merge|send|upload|open|visit|fetch|run|"
        r"execute|mark|classify|always|never|must)\b"
        r"|\b(?:ignore|disregard|forget)\b[^.\n]{0,30}\b(?:instructions?|rules?|prompts?)\b"
        r"|(?:以前|上記|これまで)の(?:指示|ルール)を(?:無視|忘れ)",
    ),
    ("url", r"(?i)\b(?:https?|ftp)://[^\s)>\]]+|\bwww\.[^\s)>\]]+"),
    (
        "shell_command",
        r"(?m)^\s*\$\s+\S.*$|\b(?:curl|wget|sudo|chmod|chown|ssh|scp)\s+\S"
        r"|\brm\s+-[a-z]*r|\|\s*(?:ba|z)?sh\b|\b(?:bash|sh|zsh)\s+-c\b",
    ),
)
_LINT = [(kind, re.compile(pattern)) for kind, pattern in LINT_RULES]


def lint(text):
    """Notes for the approver, never a refusal: agent-addressed orders, URLs, commands, secrets."""
    found = []
    for kind, pattern in _LINT:
        found += [(match.start(), match.end(), kind) for match in pattern.finditer(text)]
    for pattern in privacy._SECRETS.values():
        found += [(match.start(), match.end(), "secret_like") for match in pattern.finditer(text)]
    return [{"kind": kind, "span": [start, end]} for start, end, kind in sorted(set(found))]


def render(title, body, check=None):
    """The artifact text. The check, when present, is one fenced JSON block at the end."""
    text = f"# {' '.join(title.split())}\n\n{body.strip()}\n"
    if check is not None:
        block = json.dumps(check, ensure_ascii=False, indent=2, sort_keys=True)
        text += f"\n```{CHECK_INFO}\n{block}\n```\n"
    return text


_CHECK_BLOCK = re.compile(r"^```" + re.escape(CHECK_INFO) + r"\n(.*?)\n```[ \t]*$", re.M | re.S)
_TITLE = re.compile(r"\A# (.+)\n")


def parse(text):
    """(title, check or None, problems) from an artifact text."""
    problems = []
    title = _TITLE.match(text)
    if title is None:
        problems.append("the artifact does not start with a '# ' title")
    elif len(title.group(1).strip()) > 200:
        problems.append("the title is over 200 characters")
    blocks = _CHECK_BLOCK.findall(text)
    check = None
    if len(blocks) > 1:
        problems.append("more than one check block")
    elif blocks:
        try:
            check = json.loads(blocks[0])
        except ValueError:
            problems.append("the check block is not JSON")
        else:
            problems += records.violations("improvement-candidate", check, "check")
            if not problems:
                problems += apply_rules.check_problems(check)
    return (title.group(1).strip() if title else ""), check, problems


def directory(run_dir, candidate_id):
    if not isinstance(candidate_id, str) or not CANDIDATE_ID.fullmatch(candidate_id):
        raise InputError(f"not a candidate id: {candidate_id!r}")
    return paths.child(run_dir, DIRECTORY, candidate_id)


def ids(run_dir):
    base = paths.child(run_dir, DIRECTORY)
    if not base.is_dir():
        return []
    return sorted(child.name for child in base.iterdir() if CANDIDATE_ID.fullmatch(child.name))


def save(run_dir, record, text=None):
    folder = directory(run_dir, record["id"])
    records.validate("improvement-candidate", record)
    if text is not None:
        paths.write_private(paths.child(folder, BODY), text)
    records.write_json(paths.child(folder, RECORD), record)


def load(run_dir, candidate_id):
    """(record, artifact text). The artifact is read as it is now, which may differ from the hash."""
    folder = directory(run_dir, candidate_id)
    if not folder.is_dir() or folder.is_symlink():
        raise InputError(f"candidate {candidate_id} does not exist in run {run_dir.name}.")
    record = records.validate("improvement-candidate", records.read_json(folder / RECORD))
    body = folder / BODY
    if body.is_symlink() or not body.is_file():
        raise InputError(f"candidate {candidate_id} has no {BODY}.")
    data = body.read_bytes()
    if len(data) > MAX_BODY_BYTES:
        raise InputError(f"{candidate_id}/{BODY} is over {MAX_BODY_BYTES} bytes.")
    try:
        return record, data.decode("utf-8")
    except UnicodeDecodeError:
        raise InputError(f"{candidate_id}/{BODY} is not UTF-8 text.") from None


def content(record, text):
    """What the artifact hash covers, parsed from body.md: {form, title, check?}.

    These are the only values evaluate applies and approve records. A record whose own form,
    title or check differ from them was changed after the artifact was written: refused (exit 3).
    """
    title, check, problems = parse(text)
    if problems:
        raise BoundaryError(f"{record['id']}/body.md cannot be read: " + "; ".join(problems))
    parsed = {"form": "prose" if check is None else "declarative_check", "title": title}
    if check is not None:
        parsed["check"] = check
    kept = {key: record[key] for key in ("form", "title", "check") if key in record}
    if kept != parsed:
        raise BoundaryError(
            f"{record['id']}/candidate.json does not match its artifact (form, title or check); "
            "only body.md, which the hash covers, may define them. Nothing was used."
        )
    return parsed


def content_sha256(text):
    return records.sha256(text)


def transition(record, to, by, now, note=None):
    """Move to `to` if `TRANSITIONS` lets `by` do it, and append the history entry."""
    allowed = TRANSITIONS.get((record["status"], to))
    if allowed != by:
        raise QualityGateError(
            f"{record['id']} cannot go from {record['status']} to {to} through {by}."
        )
    entry = {
        "at": now,
        "from": record["status"],
        "to": to,
        "by": by,
        "version": record["version"],
        "content_sha256": record["artifact"]["content_sha256"],
    }
    if note:
        entry["note"] = note
    record["status"] = to
    record["history"].append(entry)
    return record


def refresh(record, text, now):
    """Bring the record in line with an edited artifact: a new version, back at proposed.

    Returns (record, problems). An artifact whose check cannot be read leaves the record as it
    was and returns the problems; the evaluation reports them and approval stays refused.
    """
    digest = content_sha256(text)
    title, check, problems = parse(text)
    if problems or digest == record["artifact"]["content_sha256"]:
        return record, problems
    record = json.loads(json.dumps(record))
    record["version"] += 1
    record["artifact"]["content_sha256"] = digest
    record["lint"] = lint(text)
    record["title"] = title
    record.pop("check", None)
    record["form"] = "prose" if check is None else "declarative_check"
    if check is not None:
        record["check"] = check
    record.update(evaluation_ref=None, criteria_hash=None, approval=None)
    transition(record, "proposed", "evaluate", now, "the artifact changed")
    return record, []
