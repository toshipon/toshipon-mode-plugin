"""Read-only bridge from the existing Doc workspace ledgers."""

import csv
import hashlib
import io
import re
from datetime import date
from pathlib import Path

LEDGERS = ("sources.tsv", "facts.md", "questions.tsv", "decisions.tsv")
SOURCE_FIELDS = ("id", "kind", "locator", "title", "retrieved", "note")
QUESTION_FIELDS = (
    "id",
    "question",
    "blocks",
    "owner",
    "status",
    "asked_at",
    "answer",
    "evidence",
    "incorporated_in",
    "updated",
)


class WorkspaceError(ValueError):
    """The workspace contract is missing or inconsistent."""


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _rows(path, fields, pattern, contents):
    with io.StringIO(contents, newline="") as stream:
        reader = csv.DictReader(stream, delimiter="\t")
        if tuple(reader.fieldnames or ()) != fields:
            raise WorkspaceError(f"{path.name} must have header {fields}.")
        rows = list(reader)
    seen = set()
    for row in rows:
        if None in row or any(value is None for value in row.values()):
            raise WorkspaceError(f"{path.name} has a malformed TSV row.")
        if not re.fullmatch(pattern, row["id"]) or row["id"] in seen:
            raise WorkspaceError(f"{path.name} has invalid or duplicate ID {row['id']}.")
        seen.add(row["id"])
    return rows


def load_workspace(workspace, input_data):
    workspace = Path(workspace).resolve()
    for name in LEDGERS:
        path = workspace / name
        if path.is_symlink() or not path.is_file():
            raise WorkspaceError(f"Missing regular workspace file {name}.")
    contents = {name: (workspace / name).read_bytes() for name in LEDGERS}
    sources = _rows(
        workspace / "sources.tsv",
        SOURCE_FIELDS,
        r"S[0-9]+",
        contents["sources.tsv"].decode("utf-8"),
    )
    questions = _rows(
        workspace / "questions.tsv",
        QUESTION_FIELDS,
        r"Q[0-9]+",
        contents["questions.tsv"].decode("utf-8"),
    )
    for source in sources:
        if source["kind"] not in {
            "confluence",
            "slack",
            "regulation",
            "code",
            "meeting",
            "interview",
            "other",
        }:
            raise WorkspaceError(f"Unknown source kind {source['kind']}.")
        if any(not source[field].strip() for field in ("locator", "title", "retrieved")):
            raise WorkspaceError(f"{source['id']} needs locator, title and retrieval date.")
        try:
            date.fromisoformat(source["retrieved"])
        except ValueError as error:
            raise WorkspaceError(f"{source['id']} has an invalid retrieval date.") from error
    source_ids = {row["id"] for row in sources}
    findings = []
    for requirement in input_data["requirements"]:
        for sid in requirement["source_refs"]:
            if sid not in source_ids:
                findings.append(
                    {
                        "id": "WORKSPACE_SOURCE",
                        "severity": "high",
                        "description": f"{requirement['id']} references unknown source {sid}.",
                    }
                )
    locators = {row["locator"] for row in sources}
    for evidence in input_data["technical_evidence"]:
        if evidence["url"] not in locators:
            findings.append(
                {
                    "id": "WORKSPACE_EVIDENCE",
                    "severity": "high",
                    "description": f"{evidence['id']} URL is absent from sources.tsv.",
                }
            )
    req_ids = {r["id"] for r in input_data["requirements"]}
    applicable = []
    for question in questions:
        if question["status"] not in {
            "open",
            "asked",
            "answered",
            "assumed",
            "incorporated",
            "dropped",
        }:
            raise WorkspaceError(f"Unknown question status {question['status']}.")
        blocks = question["blocks"]
        if req_ids.intersection(re.findall(r"REQ-[0-9]{3,}", blocks)) or re.search(
            r"\bnfr\b", blocks, re.I
        ):
            applicable.append(question)
            if question["status"] in {"answered", "incorporated"}:
                evidence_ids = set(re.findall(r"S[0-9]+", question["evidence"]))
                if (
                    not question["answer"].strip()
                    or not evidence_ids
                    or not evidence_ids.issubset(source_ids)
                ):
                    raise WorkspaceError(
                        f"{question['id']} needs an answer and known source evidence."
                    )
            if question["status"] in {"open", "asked", "assumed"}:
                findings.append(
                    {
                        "id": "WORKSPACE_QUESTION",
                        "severity": "high",
                        "description": f"{question['id']} is {question['status']} and blocks {blocks}.",
                    }
                )
    return {
        "findings": findings,
        "sources": sources,
        "questions": applicable,
        "facts": contents["facts.md"].decode("utf-8"),
        "hashes": {name: hashlib.sha256(value).hexdigest() for name, value in contents.items()},
    }


def assert_unchanged(workspace, hashes, input_path=None, input_digest=None):
    for name, expected in hashes.items():
        path = Path(workspace) / name
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise WorkspaceError(f"Workspace file changed during evaluation: {name}.")
    if input_path is not None and digest(input_path) != input_digest:
        raise WorkspaceError("Input snapshot changed during evaluation.")


def prepare_output(workspace, out):
    workspace = Path(workspace).resolve()
    raw = Path(out).absolute()
    if raw.is_symlink() or raw.resolve().parent != workspace:
        raise WorkspaceError("Output must be a new direct child directory of the workspace.")
    out = raw.resolve()
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise WorkspaceError("Output directory must be empty; use a new run directory.")
    out.mkdir(parents=False, exist_ok=True)
    for name in LEDGERS:
        (out / name).write_bytes((workspace / name).read_bytes())
    return out
