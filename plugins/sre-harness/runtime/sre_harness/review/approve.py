"""`review approve`, `review reject`, `review retire`: a person's decisions, kept in the registry.

Approval is the one step that turns a candidate into something future agents may read as an
instruction, so it is held to a terminal:

- The command line refuses to run without a TTY on stdin and stdout (exit 3). There is no flag
  or environment variable that skips this. An agent's shell has no TTY, so an agent cannot
  approve; a person at a terminal can. This is not proof of who the person is.
- The screen shows the artifact, the lint notes, every source comment with its author's type and
  association, the conflicts with existing rules and the evaluation. External text is escaped
  or fenced.
- The person types the first PREFIX_LENGTH characters of the artifact's SHA-256.
- The candidate must have been evaluated with exactly that hash (exit 3 otherwise).

Activation stops at the registry record and, when asked, a Unified Diff that adds the artifact
to a path the person names in their repository. The harness never writes into a work tree and
never commits.
"""

import re
from pathlib import PurePosixPath

from .. import paths, records
from ..errors import BoundaryError, InputError
from . import candidates, registry
from .markdown import code_block, escape

PREFIX_LENGTH = 12
_TARGET_PATH = re.compile(r"[A-Za-z0-9._/-]{1,200}")
REFUSED_ROOTS = {".git", ".github"}


def target_path(value):
    """A relative path in the person's repository, outside .git/ and .github/."""
    parts = PurePosixPath(value).parts if isinstance(value, str) else ()
    if (
        not isinstance(value, str)
        or not _TARGET_PATH.fullmatch(value)
        or value.startswith("/")
        or not parts
        or ".." in parts
        or "." in value.split("/")
        or parts[0] in REFUSED_ROOTS
    ):
        raise BoundaryError(
            f"refused activation path {value!r}: give a relative path outside .git/ and .github/."
        )
    return value


def activation_diff(path, text):
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    body = "".join(f"+{line}\n" for line in lines)
    return f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{len(lines)} @@\n{body}"


def _person(reviewer, reason):
    for name, value, limit in (("--reviewer", reviewer, 200), ("--reason", reason, 2000)):
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise InputError(f"{name} must be 1-{limit} characters.")
    return reviewer.strip(), reason.strip()


def evaluated(run_dir, record, text):
    """(evaluation, its entry for this candidate, the evaluation file's hash), or exit 3."""
    digest = candidates.content_sha256(text)
    if digest != record["artifact"]["content_sha256"]:
        raise BoundaryError(
            f"{record['id']}/body.md changed after it was evaluated; run `review evaluate` again."
        )
    path = paths.child(run_dir, "evaluation.json")
    if not path.is_file():
        raise BoundaryError(f"{record['id']} has not been evaluated; run `review evaluate` first.")
    evaluation = records.validate("review-evaluation", records.read_json(path))
    entry = next(
        (
            item
            for item in evaluation["candidates"]
            if item["id"] == record["id"]
            and item["content_sha256"] == digest
            and item["status"] == "evaluated"
        ),
        None,
    )
    if (
        entry is None
        or record["status"] != "evaluated"
        or record["criteria_hash"] != evaluation["criteria_hash"]
    ):
        raise BoundaryError(
            f"{record['id']} has no evaluation of its current content ({digest[:PREFIX_LENGTH]}…)."
        )
    return evaluation, entry, records.sha256(path.read_bytes())


def screen(record, text, evaluation, entry):
    """The approval screen as Markdown. Every external string is escaped or fenced."""
    digest = record["artifact"]["content_sha256"]
    lines = [
        f"# Approve {record['id']} (run {record['run_id']}, version {record['version']})",
        "",
        f"- title: {escape(record['title'])}",
        f"- form: {record['form']}; target: {record['target']}",
        f"- content_sha256: `{digest}`",
        f"- source_trust: {record['source_trust']}"
        + (" (admitted with --include-untrusted)" if record["untrusted_included"] else ""),
        "",
        "## Evaluation",
        "",
        f"- status: {evaluation['status']}"
        + (
            f" ({escape(evaluation['unevaluable_reason'])})"
            if evaluation["status"] != "evaluated"
            else ""
        ),
        f"- criteria_hash: `{evaluation['criteria_hash']}`; evaluation PRs: {evaluation['n_prs']}",
        f"- findings of this candidate: {entry['findings']}",
    ]
    if entry["config"] is not None:
        for name, metric in entry["config"]["metrics"].items():
            shown = metric["value"] if metric["defined"] else metric.get("reason", "undefined")
            interval = f" (95% CI {metric['ci95']})" if metric["ci95"] else ""
            lines.append(f"  - {name}: {shown}{interval}")
    lines += ["", "## Lint (notes for you, not refusals)", ""]
    if not record["lint"]:
        lines.append("- none")
    for item in record["lint"]:
        start, end = item["span"]
        lines += [f"- {item['kind']} at {start}-{end}:", "", code_block(text[start:end]), ""]
    lines += ["", "## Source comments (who wrote what the candidate rests on)", ""]
    for number, source in enumerate(record["sources"], start=1):
        who = f"author_type {source['author_type']}"
        who += f", association {source.get('author_association', 'unknown')}"
        if source.get("author_login"):
            who += f", login {escape(source['author_login'], 80)}"
        lines += [
            f"### {number}. {source['thread_id']} ({source['text_kind']}; {who})",
            "",
            code_block(source["text"]),
            "",
        ]
    lines += ["## Relations to existing rules", ""]
    if not record["conflicts"]:
        lines.append("- none found")
    for conflict in record["conflicts"]:
        lines += [
            f"- {conflict['kind']} {escape(conflict['existing_ref'], 200)}: "
            f"{escape(conflict['basis'])}",
            "",
            code_block(conflict["quote"]),
            "",
        ]
    lines += [
        "",
        "## Risk",
        "",
        code_block(record["risk"]),
        "",
        "## Rollback",
        "",
        code_block(record["rollback"]),
        "",
        "## Artifact (body.md)",
        "",
        code_block(text, "markdown"),
        "",
    ]
    return "\n".join(lines)


def _decision(reviewer, reason, now, via):
    return {"reviewer": reviewer, "reason": reason, "decided_at": now, "via": via}


def _candidate_block(record):
    block = {
        "content_sha256": record["artifact"]["content_sha256"],
        "candidate_version": record["version"],
        "form": record["form"],
        "target": record["target"],
        "title": record["title"],
        "problem": record["problem"],
        "artifact_file": "",
    }
    if "check" in record:
        block["check"] = record["check"]
    return block


def _place(root, record):
    """(entry id, existing versions) for the candidate's lineage."""
    found = registry.entries(root)
    entry_id = registry.lineage_of(found, record["run_id"], record["id"]) or registry.next_id(found)
    return entry_id, found.get(entry_id, [])


def _version(root, record, status, evaluation, decision, text, path=None):
    entry_id, versions = _place(root, record)
    number = len(versions) + 1
    block = {**_candidate_block(record), "artifact_file": registry.version_name(number, "md")}
    version = {
        "schema_version": records.SCHEMA_VERSION,
        "id": entry_id,
        "version": number,
        "created_at": decision["decided_at"],
        "status": status,
        "repo": record["repo"],
        "lineage": {"run_id": record["run_id"], "candidate_id": record["id"]},
        "candidate": block,
        "evaluation": evaluation,
        "decision": decision,
        "previous_version": number - 1 if number > 1 else None,
        "activation": None,
    }
    diff = None
    if path is not None:
        diff = activation_diff(path, text)
        version["activation"] = {
            "diff_file": registry.version_name(number, "diff"),
            "target_path": path,
            "diff_sha256": records.sha256(diff),
        }
    folder = registry.write_version(root, version, text, diff)
    return version, folder, versions


def approve(run_dir, root, candidate_id, reviewer, reason, ask, show, activate_as=None):
    """Show the screen, ask for the hash prefix, then record the approval. `ask` and `show` are
    the terminal's input and output; the command line passes them only when both are a TTY."""
    reviewer, reason = _person(reviewer, reason)
    path = None if activate_as is None else target_path(activate_as)
    record, text = candidates.load(run_dir, candidate_id)
    evaluation, entry, evaluation_sha = evaluated(run_dir, record, text)
    digest = record["artifact"]["content_sha256"]
    show(screen(record, text, evaluation, entry))
    typed = ask(f"Type the first {PREFIX_LENGTH} characters of content_sha256 to approve: ")
    if not isinstance(typed, str) or typed.strip() != digest[:PREFIX_LENGTH]:
        raise BoundaryError("the typed hash does not match the artifact; nothing was recorded.")
    _, versions = _place(root, record)
    if versions and versions[-1]["status"] == "approved":
        if versions[-1]["candidate"]["content_sha256"] == digest:
            raise InputError(f"{candidate_id} is already approved with this content.")
    now = records.iso()
    reference = {
        "ref": f"runs/{run_dir.name}/evaluation.json",
        "sha256": evaluation_sha,
        "criteria_hash": evaluation["criteria_hash"],
        "status": evaluation["status"],
        "content_sha256": entry["content_sha256"],
    }
    decision = _decision(reviewer, reason, now, "terminal_hash_prefix")
    version, folder, _ = _version(root, record, "approved", reference, decision, text, path)
    record["approval"] = {
        "reviewer": reviewer,
        "reason": reason,
        "approved_at": now,
        "candidate_sha256": digest,
        "registry_id": version["id"],
        "registry_version": version["version"],
    }
    candidates.transition(record, "approved", "approve", now)
    candidates.save(run_dir, record)
    summary = {
        "candidate": candidate_id,
        "registry_id": version["id"],
        "version": version["version"],
    }
    if path is not None:
        summary["activation_diff"] = str(folder / version["activation"]["diff_file"])
    return summary


def reject(run_dir, root, candidate_id, reviewer, reason):
    reviewer, reason = _person(reviewer, reason)
    record, text = candidates.load(run_dir, candidate_id)
    now = records.iso()
    candidates.transition(record, "rejected", "reject", now)
    decision = _decision(reviewer, reason, now, "command")
    version, _, _ = _version(root, record, "rejected", None, decision, text)
    candidates.save(run_dir, record)
    return {"candidate": candidate_id, "registry_id": version["id"], "version": version["version"]}


def retire(root, entry_id, reviewer, reason):
    """A new version marking the latest approval retired. The run's candidate follows if present."""
    reviewer, reason = _person(reviewer, reason)
    versions = registry.entries(root).get(entry_id)
    if not versions:
        raise InputError(f"registry entry {entry_id} does not exist.")
    latest = versions[-1]
    if latest["status"] != "approved":
        raise InputError(f"{entry_id} is {latest['status']}, not approved; nothing to retire.")
    text = registry.artifact(root, latest)
    now = records.iso()
    number = latest["version"] + 1
    version = {
        **latest,
        "version": number,
        "created_at": now,
        "status": "retired",
        "candidate": {**latest["candidate"], "artifact_file": registry.version_name(number, "md")},
        "decision": _decision(reviewer, reason, now, "command"),
        "previous_version": latest["version"],
        "activation": None,
    }
    registry.write_version(root, version, text)
    lineage = latest["lineage"]
    run = paths.child(root, "runs", lineage["run_id"])
    if run.is_dir() and candidates.directory(run, lineage["candidate_id"]).is_dir():
        record, _ = candidates.load(run, lineage["candidate_id"])
        if record["status"] == "approved":
            candidates.transition(record, "retired", "retire", now)
            candidates.save(run, record)
    return {"registry_id": entry_id, "version": number, "status": "retired"}
