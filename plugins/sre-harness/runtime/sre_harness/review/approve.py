"""`review approve`, `review reject`, `review retire`: a person's decisions, kept in the registry.

Approval is the one step that turns a candidate into something future agents may read as an
instruction, so it is held to a terminal:

- The command line refuses to run without a TTY on stdin and stdout (exit 3), for approve and
  retire. There is no flag or environment variable that skips this. It stops an approval from
  a pipe or a non-interactive run by accident; a program that opens a pty gets through, so it
  proves neither that a person is there nor who they are. The approval is trusted through the
  review of the repository the person commits the registry's diff to.
- The screen shows what will be recorded (form, title and check parsed from body.md), the
  artifact, its lint notes, every source comment with its author's type and association, the
  conflicts with existing rules and the evaluation. External text is escaped or fenced.
- Only body.md, which the hash covers, defines what is recorded. A candidate.json whose form,
  title or check disagree with it is refused (exit 3).
- The person types the first PREFIX_LENGTH characters of the artifact's SHA-256.
- The evaluation, the run's split.json and the split hash analyze and generate recorded must
  agree, and the evaluation's criteria_hash must be the one its split and criteria give (exit
  3). This catches approving on a stale or mismatched evaluation by mistake. It is no defence
  against someone who edits the run's files: they are the user's own files, with no signature.
- The candidate must have been evaluated with exactly that hash, and the evaluation must have
  status `evaluated`: an `unevaluable` one (fewer than 30 evaluation PRs) approves nothing
  (exit 3 otherwise).

Activation stops at the registry record and, when asked, a Unified Diff that adds the artifact
to a path the person names in their repository. The harness never writes into a work tree and
never commits.
"""

import json
import re
from pathlib import PurePosixPath

from .. import paths, records
from ..errors import BoundaryError, InputError
from ..knowledge import block as knowledge_block
from ..knowledge import config as knowledge_config
from . import candidates, metrics, registry
from . import split as split_stage
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


def check_consistent(run_dir, evaluation):
    """The evaluation, the split and the stages that used it must name the same split."""
    try:
        split = split_stage.load(paths.child(run_dir, split_stage.FILE))
    except InputError as error:
        raise BoundaryError(f"the run's split.json cannot back an approval: {error}") from None
    expected = metrics.criteria_hash(
        split["sha256"],
        evaluation["criteria"]["metrics"],
        evaluation["criteria"]["thresholds"],
        evaluation["criteria"]["labels"],
    )
    found = {"evaluation.json": evaluation["split_sha256"]}
    for name in ("analyze.json", "generate.json"):
        found[name] = records.read_json(paths.child(run_dir, name)).get("split_sha256")
    stale = sorted(name for name, value in found.items() if value != split["sha256"])
    if evaluation["criteria_hash"] != expected:
        stale.append("evaluation.json criteria_hash")
    if stale:
        raise BoundaryError(
            f"{', '.join(stale)} do not match the run's split.json; run the stages again with "
            "this split before approving. Nothing was recorded."
        )


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
    check_consistent(run_dir, evaluation)
    if evaluation["status"] != "evaluated":
        raise BoundaryError(
            f"the evaluation is {evaluation['status']} ({evaluation['unevaluable_reason']}); "
            "nothing can be approved on it. Split a period with enough evaluation PRs."
        )
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


def recorded(content):
    """The values the registry records besides the hash, as the screen shows them."""
    return json.dumps(content, ensure_ascii=False, indent=2, sort_keys=True)


def screen(record, text, content, evaluation, entry, knowledge=None):
    """The approval screen as Markdown. Every external string is escaped or fenced.

    `content` is what the registry will record (parsed from body.md, which the hash covers).
    `knowledge`, when given, is the knowledge block it will record as well; the typed hash
    then covers both. The rest comes from candidate.json: context for the decision, not
    recorded.
    """
    digest = candidates.content_sha256(text)
    lines = [
        f"# Approve {record['id']} (run {record['run_id']}, version {record['version']})",
        "",
        f"- content_sha256: `{digest}`",
        "",
        "## What is recorded (parsed from body.md; the hash covers it)",
        "",
        code_block(recorded(content), "json"),
        "",
    ]
    if knowledge is not None:
        lines += knowledge_block.describe(knowledge, digest)
    lines += [
        "## Context from candidate.json (not covered by the hash, not recorded)",
        "",
        f"- target: {record['target']}",
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
        f"- criteria_hash: `{evaluation['criteria_hash']}`; evaluation PRs: {evaluation['n_prs']} "
        f"({evaluation['n_prs_with_units']} with hunks)",
        f"- findings of this candidate: {entry['findings']}",
    ]
    if entry["config"] is not None:
        for name, metric in entry["config"]["metrics"].items():
            shown = metric["value"] if metric["defined"] else metric.get("reason", "undefined")
            interval = f" (95% CI {metric['ci95']})" if metric["ci95"] else ""
            lines.append(f"  - {name}: {shown}{interval}")
    lines += ["", "## Lint of body.md (notes for you, not refusals)", ""]
    notes = candidates.lint(text)
    if not notes:
        lines.append("- none")
    for item in notes:
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


def _place(root, run_dir, candidate_id):
    """(entry id, existing versions) for the candidate's lineage (run id + candidate id)."""
    found = registry.entries(root)
    entry_id = registry.lineage_of(found, run_dir.name, candidate_id) or registry.next_id(found)
    return entry_id, found.get(entry_id, [])


def _generation(split_path):
    """What the rule was learned from, so later evaluations can keep it off those PRs."""
    if not split_path.is_file():
        return {}
    split = records.read_json(split_path)
    return {"generation": {"prs": split["generation"]["prs"], "t1": split["t1"]}}


def _repo(run_dir):
    return records.read_json(paths.child(run_dir, "collect.json"))["repo"]


def _version(
    root,
    run_dir,
    candidate_id,
    content,
    status,
    evaluation,
    decision,
    text,
    path=None,
    knowledge=None,
):
    """Write one registry version. Every recorded value comes from body.md (`content`, the
    hash), the run (repository, lineage) or the person (decision, the knowledge block); none
    from candidate.json. Without a knowledge block the version has no such key."""
    entry_id, versions = _place(root, run_dir, candidate_id)
    number = len(versions) + 1
    block = {
        "content_sha256": candidates.content_sha256(text),
        **content,
        "artifact_file": registry.version_name(number, "md"),
    }
    split_path = paths.child(run_dir, split_stage.FILE)
    version = {
        "schema_version": records.SCHEMA_VERSION,
        "id": entry_id,
        "version": number,
        "created_at": decision["decided_at"],
        "status": status,
        "repo": _repo(run_dir),
        "lineage": {"run_id": run_dir.name, "candidate_id": candidate_id},
        **_generation(split_path),
        "candidate": block,
        "evaluation": evaluation,
        "decision": decision,
        "previous_version": number - 1 if number > 1 else None,
        "activation": None,
    }
    if knowledge is not None:
        version[knowledge_block.DEFINITION] = knowledge
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


def approve(
    run_dir, root, candidate_id, reviewer, reason, ask, show, activate_as=None, knowledge=None
):
    """Show the screen, ask for the hash prefix, then record the approval. `ask` and `show` are
    the terminal's input and output; the command line passes them only when both are a TTY.
    `knowledge` (knowledge.block.Options) places the rule in the knowledge index; with no
    option given, the version is written exactly as without it."""
    reviewer, reason = _person(reviewer, reason)
    path = None if activate_as is None else target_path(activate_as)
    record, text = candidates.load(run_dir, candidate_id)
    evaluation, entry, evaluation_sha = evaluated(run_dir, record, text)
    content = candidates.content(record, text)
    digest = candidates.content_sha256(text)
    placed = None
    if knowledge is not None and knowledge.given():
        placed = knowledge_block.build(knowledge_config.load(root), _repo(run_dir), knowledge)
    show(screen(record, text, content, evaluation, entry, placed))
    name = "content_sha256" if placed is None else "confirmation_sha256"
    typed = ask(f"Type the first {PREFIX_LENGTH} characters of {name} to approve: ")
    expected = knowledge_block.confirmation(digest, placed)
    if not isinstance(typed, str) or typed.strip() != expected[:PREFIX_LENGTH]:
        what = "the artifact" if placed is None else "the artifact and the knowledge block"
        raise BoundaryError(f"the typed hash does not match {what}; nothing was recorded.")
    _, versions = _place(root, run_dir, candidate_id)
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
    version, folder, _ = _version(
        root,
        run_dir,
        candidate_id,
        content,
        "approved",
        reference,
        decision,
        text,
        path,
        placed,
    )
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
    content = candidates.content(record, text)
    now = records.iso()
    candidates.transition(record, "rejected", "reject", now)
    decision = _decision(reviewer, reason, now, "command")
    version, _, _ = _version(root, run_dir, candidate_id, content, "rejected", None, decision, text)
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
    # The knowledge block stays on the approved version; the entry keeps that placement.
    version.pop(knowledge_block.DEFINITION, None)
    registry.write_version(root, version, text)
    lineage = latest["lineage"]
    run = paths.child(root, "runs", lineage["run_id"])
    if run.is_dir() and candidates.directory(run, lineage["candidate_id"]).is_dir():
        record, _ = candidates.load(run, lineage["candidate_id"])
        if record["status"] == "approved":
            candidates.transition(record, "retired", "retire", now)
            candidates.save(run, record)
    return {"registry_id": entry_id, "version": number, "status": "retired"}
