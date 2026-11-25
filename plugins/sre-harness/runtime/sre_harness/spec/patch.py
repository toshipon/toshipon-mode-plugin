"""Candidate fixes as Unified Diff files, the gate every diff passes, and `spec check-patch`.

What gets a patch. Fixed rules only; no model writes a diff in this version:

- `contradiction` whose registry has a value for (entity, key): every `- key: value` bullet or
  front-matter `key: value` line the finding cites gets the registry's value.
- `superseded_dependency`: the line that names the superseded decision names its successor
  instead, when the chain of `superseded_by` leads to exactly one approved, active decision.
  A finding that rests on the registry (`requirements.json#REQ-0004`) is located through the
  requirement's source section in `documents.jsonl`.

Nothing else is patched: which value of a `semantic_conflict` is right, or where a
`not_reflected` entity belongs, is for people to decide.

The gate (`admit`) is the same for every diff, whatever wrote it:

1. `screen`: at most `MAX_PATCH_BYTES` (over it stops, nothing is cut), UTF-8 with LF only,
   and a strict reading of the headers. Each file must be `diff --git a/P b/P`, an optional
   `index <a>..<b> 100644|100755`, `--- a/P`, `+++ b/P` and whole hunks. Quoted names,
   another name on either side, `/dev/null`, a tab after a name, rename / copy / mode / new /
   deleted headers, binary patches and any text git might read as another patch are refused,
   so the path read here is the path git applies to. Every path must pass `path_refusal`.
2. In the commit the patch is for, each path must be a regular file (not a symlink, not a
   submodule).
3. A temporary index: `git read-tree <sha>` into a new index file, then
   `git apply --cached --check <patch>` against that index. No work tree is made; the bare
   repository's refs and its own index are not touched.

A diff that fails (1) or (2) is never written. One that fails (3) is not written either; the
reason goes to `impact-report.json`'s `patch_rejections`. The harness never applies a patch.
"""

import difflib
import fnmatch
import hashlib
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths, records
from ..errors import BoundaryError, InputError, QualityGateError
from . import analyze as analyze_stage
from . import prs as git_prs
from . import structure, trace
from .ingest_file import TEXT_KINDS

MAX_PATCH_BYTES = 1024 * 1024
PATCHABLE = ("contradiction", "superseded_dependency")
REGULAR_MODES = ("100644", "100755")
NOTE = "the harness does not apply patches; review the diff and apply it yourself."

_PATCH_ID = re.compile(r"PT-[0-9]{4,}")
_SEGMENT = re.compile(r"[A-Za-z0-9._+-]+")
_LOCATION = re.compile(r"([0-9a-f]{40}):([^#]+)#L([0-9]+)")
_LINES = re.compile(r"L([0-9]+)-L([0-9]+)")
_HUNK = re.compile(r"@@ -[0-9]+(?:,([0-9]+))? \+[0-9]+(?:,([0-9]+))? @@(?: .*)?")
_INDEX = re.compile(r"index [0-9a-f]{7,40}\.\.[0-9a-f]{7,40}(?: ([0-7]{6}))?")
_EXTENDED = (
    "old mode ",
    "new mode ",
    "deleted file mode ",
    "new file mode ",
    "copy from ",
    "copy to ",
    "rename from ",
    "rename to ",
    "similarity index ",
    "dissimilarity index ",
)


class Refused(Exception):
    """A diff or a path the gate does not let through. `code` is a fixed word."""

    def __init__(self, code, detail=""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = str(detail)[:500]


def patch_id(value):
    """`PT-0001`. Anything else (a path, a file name) is refused before a file is opened."""
    if not isinstance(value, str) or not _PATCH_ID.fullmatch(value):
        raise BoundaryError(f"refused --patch {value!r}: give a patch id such as PT-0001.")
    return value


def blob_sha(data):
    """The git object id (SHA-1) of a blob with these bytes."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# Paths ------------------------------------------------------------------------------------


def glob_match(pattern, path):
    """Segment by segment: `*` stays inside one segment, `**` spans zero or more."""

    def match(parts, names):
        if not parts:
            return not names
        if parts[0] == "**":
            return any(match(parts[1:], names[index:]) for index in range(len(names) + 1))
        return (
            bool(names) and fnmatch.fnmatchcase(names[0], parts[0]) and match(parts[1:], names[1:])
        )

    return match(pattern.split("/"), path.split("/"))


@dataclass(frozen=True)
class Allow:
    """What may be patched: the manifest's text documents and the manifest's `patch_globs`."""

    documents: frozenset = field(default_factory=frozenset)
    globs: tuple = ()

    @classmethod
    def from_run(cls, run_dir):
        ingest = records.read_json(paths.child(run_dir, "ingest.json"))
        rows = records.read_jsonl(paths.child(run_dir, "documents.jsonl"))
        documents = frozenset(
            row["provenance"]["locator"]
            for row in rows
            if row["kind"] in TEXT_KINDS and row["provenance"]["source_id"].startswith("file:")
        )
        return cls(documents, tuple(ingest.get("patch_globs", [])))

    def allows(self, path):
        return path in self.documents or any(glob_match(glob, path) for glob in self.globs)


def path_refusal(path, allow):
    """(code, detail) when `path` may not be patched, None when it may."""
    if not isinstance(path, str) or not path or path.startswith(("/", "~")):
        return "not_a_plain_path", f"{path!r} is empty or absolute"
    parts = path.split("/")
    if any(part in (".", "..") for part in parts):
        return "dotdot", f"{path!r} has a '.' or '..' segment"
    if not all(_SEGMENT.fullmatch(part) for part in parts):
        return "not_a_plain_path", f"{path!r} has a character outside A-Z a-z 0-9 . _ + -"
    for part in parts:
        lower = part.lower()
        if lower.startswith(".github"):
            return "github_dir", f"{path}: .github/ is never patched"
        if lower.startswith(".git"):
            return "git_dir", f"{path}: .git and .git* files are never patched"
    if structure.file_kind(path) != "markdown":
        return "iac_or_code", f"{path}: only Markdown documents are patched, never IaC or code"
    if not allow.allows(path):
        return "not_allowed", f"{path} is neither a manifest document nor under patch_globs"
    return None


# The header parser ------------------------------------------------------------------------


def _git_header(rest):
    if '"' in rest:
        raise Refused("quoted_path", rest[:200])
    if not rest.startswith("a/"):
        raise Refused("no_prefix", rest[:200])
    size = (len(rest) - 5) // 2
    path = rest[2 : 2 + size]
    if size < 1 or rest != f"a/{path} b/{path}":
        raise Refused("header_mismatch", rest[:200])
    return path


def _hunk(lines, index):
    found = _HUNK.fullmatch(lines[index])
    if found is None:
        raise Refused("bad_hunk", lines[index][:200])
    old = int(found.group(1)) if found.group(1) is not None else 1
    new = int(found.group(2)) if found.group(2) is not None else 1
    index += 1
    while old > 0 or new > 0:
        if index >= len(lines):
            raise Refused("truncated_hunk", "the patch ends inside a hunk")
        line = lines[index]
        if line.startswith(" "):
            old, new = old - 1, new - 1
        elif line.startswith("-"):
            old -= 1
        elif line.startswith("+"):
            new -= 1
        elif not line.startswith("\\ "):
            raise Refused("truncated_hunk", f"line {index + 1} is not part of the hunk")
        if old < 0 or new < 0:
            raise Refused("bad_hunk", f"line {index + 1} runs past the hunk's counts")
        index += 1
    while index < len(lines) and lines[index].startswith("\\ "):
        index += 1
    return index


def parse(data):
    """The paths a Unified Diff changes, read the way `git apply` reads it, or Refused."""
    if len(data) > MAX_PATCH_BYTES:
        raise QualityGateError(
            f"the patch is {len(data)} bytes, over the {MAX_PATCH_BYTES}-byte cap; stopped "
            "instead of cutting it."
        )
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Refused("not_a_plain_text", "the patch is not UTF-8") from None
    if "\r" in text or "\0" in text or (text and not text.endswith("\n")):
        raise Refused("not_a_plain_text", "CR, NUL or no final newline")
    lines = text.split("\n")[:-1]
    found, index = [], 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("diff --git "):
            raise Refused("unexpected_line", f"line {index + 1}: {line[:100]}")
        path = _git_header(line[len("diff --git ") :])
        if path in found:
            raise Refused("path_twice", path)
        index += 1
        while index < len(lines) and not lines[index].startswith(("--- ", "@@", "diff --git ")):
            header = lines[index]
            if header.startswith(("GIT binary patch", "Binary files ")):
                raise Refused("binary", path)
            if header.startswith(_EXTENDED):
                raise Refused("extended_header", header[:200])
            mode = _INDEX.fullmatch(header)
            if mode is None:
                raise Refused("unexpected_line", f"line {index + 1}: {header[:100]}")
            if mode.group(1) not in REGULAR_MODES:
                raise Refused("mode", f"{path}: {header[:100]}")
            index += 1
        if index + 1 >= len(lines) or not lines[index].startswith("--- "):
            raise Refused("no_hunk", path)
        old, new = lines[index], lines[index + 1]
        if "/dev/null" in (old[4:], new[4:]):
            raise Refused("new_or_deleted_file", path)
        if old != f"--- a/{path}" or new != f"+++ b/{path}":
            raise Refused("header_mismatch", f"{old[:100]} / {new[:100]}")
        index += 2
        hunks = 0
        while index < len(lines) and lines[index].startswith("@@"):
            index = _hunk(lines, index)
            hunks += 1
        if not hunks:
            raise Refused("no_hunk", path)
        found.append(path)
    if not found:
        raise Refused("no_hunk", "the patch changes no file")
    return found


def screen(data, allow):
    """`parse`, then the allowlist for every path. Returns the paths."""
    found = parse(data)
    for path in found:
        refusal = path_refusal(path, allow)
        if refusal is not None:
            raise Refused(*refusal)
    return found


def admit(data, repository, sha, allow, workdir):
    """The whole gate for one diff against commit `sha`. Returns {paths, preimage_blob_shas,
    apply_check}; Refused when a header or a path is not allowed.

    `workdir` holds the temporary index and the candidate file; the caller removes it."""
    found = screen(data, allow)
    preimage = {}
    for path in found:
        entry = repository.entry(sha, path)
        if entry is None:
            raise Refused("path_not_at_target", f"{path} is not a file at {sha[:12]}")
        mode, blob = entry
        if mode not in REGULAR_MODES:
            raise Refused("mode", f"{path} is mode {mode} at {sha[:12]}")
        preimage[path] = blob
    workdir = Path(workdir)
    candidate = paths.write_private(paths.child(workdir, "candidate.diff"), data)
    index = paths.child(workdir, "index")
    repository.run(["read-tree", sha], index_file=index)
    code, _ = repository.run(
        ["apply", "--cached", "--check", str(candidate)], allowed=range(0, 256), index_file=index
    )
    return {
        "paths": found,
        "preimage_blob_shas": preimage,
        "apply_check": "passed" if code == 0 else "failed",
    }


# Candidates from findings ----------------------------------------------------------------


def _lines(text):
    """Lines with their "\n", cut where git cuts them (not at U+2028, \x0c, ... as
    `str.splitlines` does)."""
    lines = [line + "\n" for line in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


def render(path, before, after, mode, preimage):
    """One file's diff in the form `parse` accepts."""
    body = "".join(
        difflib.unified_diff(
            _lines(before),
            _lines(after),
            f"a/{path}",
            f"b/{path}",
            n=3,
        )
    )
    postimage = blob_sha(after.encode("utf-8"))
    return f"diff --git a/{path} b/{path}\nindex {preimage}..{postimage} {mode}\n{body}"


def successor(registry, name):
    """The approved decision the chain of single successors from `name` ends at, or None.
    An approved decision is the active end of a chain (`registry` sets `superseded` only
    when every successor was approved)."""
    seen, current = {name}, name
    while registry.status(current) == "superseded":
        following = registry.decisions[current].get("superseded_by") or []
        if len(following) != 1 or following[0] in seen or following[0] not in registry.decisions:
            return None
        current = following[0]
        seen.add(current)
    item = registry.decisions.get(current)
    if current == name or item is None or item["status"] != "approved":
        return None
    return current


class Context:
    """What the candidates are read against: the registry, its facts, the documents."""

    def __init__(self, run_dir):
        decisions = records.read_json(paths.child(run_dir, "decisions.json"))["decisions"]
        requirements = records.read_json(paths.child(run_dir, "requirements.json"))
        self.documents = records.read_jsonl(paths.child(run_dir, "documents.jsonl"))
        self.registry = trace.Registry(decisions, requirements["requirements"])
        self.facts = analyze_stage.registry_facts(self.registry, self.documents)
        self.by_id = {row["id"]: row for row in self.documents}


def _target(item, report):
    """(target PR or None, target SHA or None, refusal or None)."""
    if not item["prs"]:
        return None, report["default_branch"]["sha"], None
    if len(item["prs"]) != 1:
        return None, None, ("several_prs", "a fix for several PRs is not offered")
    number = item["prs"][0]
    pr = next((pr for pr in report["prs"] if pr["number"] == number), None)
    if (
        pr is None
        or pr["stale"]
        or pr.get("missing")
        or pr["analyzed_head_sha"] is None
        or pr["analyzed_head_sha"] != pr["head_sha"]
    ):
        return number, None, ("stale_sha", f"PR #{number} was not analyzed at its listed head")
    return number, pr["analyzed_head_sha"], None


def _cited(item, sha):
    """[(path, line)] of the evidence that names a file line, and refusals for the others."""
    out, refusals = [], []
    for entry in item["evidence"]:
        found = _LOCATION.fullmatch(entry["ref"])
        if found is None:
            continue
        if found.group(1) != sha:
            refusals.append((found.group(2), "evidence_not_at_target", entry["ref"][:200]))
            continue
        out.append((found.group(2), int(found.group(3))))
    return out, refusals


def _value_line(line, key, value):
    """The line with the value of `key` replaced, or None when the line does not state it."""
    for pattern in (
        rf"([ \t]*[-*][ \t]+{re.escape(key)}[ \t]*:[ \t]+)(.*?)([ \t]*)",
        rf"({re.escape(key)}[ \t]*:[ \t]*)(.*?)([ \t]*)",
    ):
        found = re.fullmatch(pattern, line)
        if found:
            old = found.group(2)
            quote = old[0] if len(old) >= 2 and old[0] == old[-1] and old[0] in "\"'" else ""
            return f"{found.group(1)}{quote}{value}{quote}{found.group(3)}"
    return None


def _token(written):
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(written)}(?![0-9])")


def _contradiction(item, context, sha):
    """(edits {path: [(line, rule)]}, refusals)."""
    if not item["entities"] or "key" not in item:
        return {}, [(None, "no_registry_value", "the finding names no entity and key")]
    expected = context.facts.get((item["entities"][0], item["key"]))
    if expected is None:
        return {}, [(None, "no_registry_value", "files disagree and the registry has no value")]
    cited, refusals = _cited(item, sha)
    edits = {}
    for path, line in cited:
        edits.setdefault(path, []).append((line, ("value", item["key"], expected[0])))
    return edits, refusals


def reference_rule(registry, old, new):
    """Move the id a file writes for `old` (its `external_id`) to the one for `new`. The
    harness's own numbers (`DEC-…`) are never what a file names, so they are not replaced."""
    return ("reference", {registry.external(old): registry.external(new)})


def _superseded(item, context, sha, repository, allow):
    registry = context.registry
    old = next((name for name in item["entities"] if registry.status(name) == "superseded"), None)
    new = successor(registry, old) if old else None
    if new is None:
        return {}, [(None, "no_single_successor", f"{old} has no single active successor")]
    rule = reference_rule(registry, old, new)
    cited, refusals = _cited(item, sha)
    edits = {}
    for path, line in cited:
        edits.setdefault(path, []).append((line, rule))
    for entry in item["evidence"]:
        kind, _, name = entry["ref"].partition("#")
        if kind != "requirements.json" or name not in registry.requirements:
            continue
        source = registry.requirements[name]["evidence"]
        document_id, _, section_id = source[0]["ref"].partition("#") if source else ("", "", "")
        document = context.by_id.get(document_id)
        if document is None or not document["provenance"]["source_id"].startswith("file:"):
            refusals.append((None, "reference_not_found", f"{name} has no source file"))
            continue
        path = document["provenance"]["locator"]
        reason = path_refusal(path, allow)
        if reason is not None:
            refusals.append((path, *reason))
            continue
        section = next((s for s in document["sections"] if s["id"] == section_id), None)
        span = _LINES.fullmatch(section["locator"]) if section else None
        if span is None:
            refusals.append((path, "reference_not_found", f"{name}: no line range"))
            continue
        text = repository.blob(sha, path) or ""
        lines = text.split("\n")
        tokens = [_token(written) for written in rule[1]]
        for number in range(int(span.group(1)), min(int(span.group(2)), len(lines)) + 1):
            if any(token.search(lines[number - 1]) for token in tokens):
                edits.setdefault(path, []).append((number, rule))
    return edits, refusals


def _apply_rule(line, rule):
    if rule[0] == "value":
        return _value_line(line, rule[1], rule[2])
    changed = line
    for written, replacement in rule[1].items():
        changed = _token(written).sub(replacement, changed)
    return changed if changed != line else None


def _edited(repository, sha, path, edits):
    """(before, after, mode, blob) of one file, or Refused."""
    entry = repository.entry(sha, path)
    if entry is None:
        raise Refused("path_not_at_target", f"{path} is not a file at {sha[:12]}")
    mode, blob = entry
    if mode not in REGULAR_MODES:
        raise Refused("mode", f"{path} is mode {mode}")
    before = repository.blob(sha, path)
    if before is None:
        raise Refused("not_utf8", path)
    if not before.endswith("\n"):
        raise Refused("no_final_newline", path)
    lines = before.split("\n")
    for number, rule in sorted(set((n, _freeze(r)) for n, r in edits)):
        if not 1 <= number < len(lines):
            raise Refused("line_changed", f"{path}#L{number} is not in the file")
        changed = _apply_rule(lines[number - 1], _thaw(rule))
        if changed is None:
            raise Refused("line_changed", f"{path}#L{number} no longer states what was found")
        lines[number - 1] = changed
    return before, "\n".join(lines), mode, blob


def _freeze(rule):
    return (rule[0], tuple(sorted(rule[1].items()))) if rule[0] == "reference" else rule


def _thaw(rule):
    return (rule[0], dict(rule[1])) if rule[0] == "reference" else rule


# The stage --------------------------------------------------------------------------------


def _clear(directory):
    if directory.is_dir():
        for path in directory.iterdir():
            stem = path.name.rsplit(".", 1)[0]
            if _PATCH_ID.fullmatch(stem) and path.suffix in (".diff", ".json"):
                path.unlink()


def _rejection(item, target_pr, sha, path, code, detail):
    value = {"finding_id": item["id"], "target_pr": target_pr, "reason": code, "detail": detail}
    if sha:
        value["target_head_sha"] = sha
    if path is not None:
        value["path"] = path[:300]
    return value


def generate(run_dir, report, open_repository):
    """(patches, rejections) for the report's findings. Passed patches are written to
    `patches/<PT-id>.diff` and `.json`; nothing else is written. `open_repository()` gives the
    bare repository `spec analyze` used; it is opened only when a finding can be patched."""
    directory = paths.child(run_dir, "patches")
    _clear(directory)
    candidates = [item for item in report["findings"] if item["kind"] in PATCHABLE]
    if not candidates:
        return [], []
    context, allow, repository = Context(run_dir), Allow.from_run(run_dir), open_repository()
    patches, rejections = [], []
    branch = report["default_branch"]["name"]
    for item in sorted(candidates, key=lambda finding: finding["id"]):
        target_pr, sha, refusal = _target(item, report)
        if refusal is not None or sha is None:
            code, detail = refusal or ("stale_sha", "no target")
            rejections.append(_rejection(item, target_pr, None, None, code, detail))
            continue
        if item["kind"] == "contradiction":
            edits, refused = _contradiction(item, context, sha)
        else:
            edits, refused = _superseded(item, context, sha, repository, allow)
        rejections += [_rejection(item, target_pr, sha, *entry) for entry in refused]
        rendered = []
        for path, changes in sorted(edits.items()):
            reason = path_refusal(path, allow)
            if reason is not None:
                rejections.append(_rejection(item, target_pr, sha, path, *reason))
                continue
            try:
                before, after, mode, blob = _edited(repository, sha, path, changes)
            except Refused as error:
                rejections.append(_rejection(item, target_pr, sha, path, error.code, error.detail))
                continue
            if after != before:
                rendered.append(render(path, before, after, mode, blob))
        if not rendered:
            continue
        data = "".join(rendered).encode("utf-8")
        try:
            with tempfile.TemporaryDirectory(prefix=".patch-check-", dir=run_dir) as work:
                admitted = admit(data, repository, sha, allow, Path(work))
        except Refused as error:
            rejections.append(_rejection(item, target_pr, sha, None, error.code, error.detail))
            continue
        if admitted["apply_check"] != "passed":
            detail = f"git apply --cached --check refused it at {sha[:12]}"
            rejections.append(_rejection(item, target_pr, sha, None, "apply_check_failed", detail))
            continue
        name = records.record_id("PT", len(patches) + 1)
        entry = {
            "id": name,
            "finding_id": item["id"],
            "target_pr": target_pr,
            "target_ref": f"refs/pull/{target_pr}/head" if target_pr else f"refs/heads/{branch}",
            "target_head_sha": sha,
            "paths": admitted["paths"],
            "preimage_blob_shas": admitted["preimage_blob_shas"],
            "apply_check": "passed",
            "content_sha256": records.sha256(data),
            "diff": f"patches/{name}.diff",
        }
        sidecar = {
            "schema_version": records.SCHEMA_VERSION,
            "run_id": report["run_id"],
            "created_at": records.iso(),
            "finding_kind": item["kind"],
            **entry,
        }
        records.validate("spec-patch", sidecar)
        paths.write_private(paths.child(directory, f"{name}.diff"), data)
        records.write_json(paths.child(directory, f"{name}.json"), sidecar)
        patches.append(entry)
    return patches, rejections


def _sidecar(run_dir, name):
    sidecar_path = paths.child(run_dir, "patches", f"{name}.json")
    diff_path = paths.child(run_dir, "patches", f"{name}.diff")
    for path in (sidecar_path, diff_path):
        if path.is_symlink():
            raise BoundaryError(f"refused to follow a symlink: {path.name}")
        if not path.is_file():
            raise InputError(f"the run has no patch {name}: run `spec report` first.")
    sidecar = records.read_json(sidecar_path)
    records.validate("spec-patch", sidecar)
    if sidecar["id"] != name:
        raise BoundaryError(f"{sidecar_path.name} describes {sidecar['id']}, not {name}.")
    data = diff_path.read_bytes()
    if len(data) > MAX_PATCH_BYTES:
        raise QualityGateError(f"{diff_path.name} is over {MAX_PATCH_BYTES} bytes; stopped.")
    if records.sha256(data) != sidecar["content_sha256"]:
        raise BoundaryError(
            f"{diff_path.name} is not the patch `spec report` wrote (content_sha256 differs)."
        )
    return sidecar, data


# What `spec report` recorded about a patch in `impact-report.json` and what the sidecar
# says must agree on these, so rewriting one file does not re-target an old patch.
BOUND_FIELDS = (
    "id",
    "target_pr",
    "target_head_sha",
    "content_sha256",
    "paths",
    "preimage_blob_shas",
)


def _reported(run_dir, name, sidecar):
    """Refuse a sidecar that differs from the report's `patches[]` entry with the same id."""
    value = records.read_json(paths.child(run_dir, "impact-report.json"))
    entry = next((item for item in value.get("patches", []) if item.get("id") == name), None)
    if entry is None:
        raise BoundaryError(f"impact-report.json lists no patch {name}; it is not checked.")
    differ = [key for key in BOUND_FIELDS if entry.get(key) != sidecar.get(key)]
    if differ:
        raise BoundaryError(
            f"{name}: the sidecar differs from impact-report.json in {', '.join(differ)}; "
            "run `spec analyze` and `spec report` again."
        )


def check(run_dir, name, context):
    """`spec check-patch`: the current head against the patch's target, then the gate again.

    `context` carries transport, limiter, source, token, root. Returns (output, exit code)."""
    name = patch_id(name)
    sidecar, data = _sidecar(run_dir, name)
    _reported(run_dir, name, sidecar)
    allow = Allow.from_run(run_dir)
    try:
        found = screen(data, allow)
    except Refused as error:
        raise BoundaryError(f"{name} does not pass the patch gate: {error}") from None
    if found != sidecar["paths"]:
        raise BoundaryError(f"{name}: the sidecar names other paths than the patch changes.")
    findings = records.read_json(paths.child(run_dir, "findings.json"))
    owner, _, repo = findings["repo"].partition("/")
    branch = git_prs.branch_name(findings["default_branch"]["name"])
    repository = git_prs.Repository.open(
        context["root"], owner, repo, context["source"], context.get("token"), host=context["host"]
    )
    target_pr = sidecar["target_pr"]
    if target_pr is None:
        repository.fetch_open(branch, [])
        current = repository.rev_parse(git_prs.main_ref(branch))
        where = f"the default branch {branch}"
    else:
        pulls, _ = git_prs.list_open(context["transport"], context["limiter"], owner, repo)
        listed = next((pull for pull in pulls if pull["number"] == target_pr), None)
        if listed is None:
            raise BoundaryError(f"PR #{target_pr} is not open any more; {name} is not checked.")
        if target_pr not in repository.fetch_open(branch, [target_pr]):
            raise QualityGateError(f"the head of PR #{target_pr} could not be fetched.")
        current = repository.rev_parse(git_prs.pull_ref(target_pr))
        where = f"PR #{target_pr}"
        if current != listed["head_sha"]:
            raise BoundaryError(
                f"the head of {where} moved between the PR list and the fetch; run "
                "`spec analyze` and `spec report` again."
            )
    if current != sidecar["target_head_sha"]:
        raise BoundaryError(
            f"{name} was made for {where} at {sidecar['target_head_sha']}, which is now "
            f"{current}. A fix for an old SHA is not offered: run `spec analyze` and "
            "`spec report` again."
        )
    try:
        with tempfile.TemporaryDirectory(prefix=".patch-check-", dir=run_dir) as work:
            admitted = admit(data, repository, current, allow, Path(work))
    except Refused as error:
        raise BoundaryError(f"{name} does not pass the patch gate: {error}") from None
    if admitted["preimage_blob_shas"] != sidecar["preimage_blob_shas"]:
        raise BoundaryError(
            f"{name}: the files at the current head differ from the ones the patch was made "
            "for (preimage_blob_shas)."
        )
    output = {
        "patch": name,
        "finding_id": sidecar["finding_id"],
        "target_pr": target_pr,
        "target_head_sha": sidecar["target_head_sha"],
        "current_head_sha": current,
        "paths": admitted["paths"],
        "apply_check": admitted["apply_check"],
        "applied": False,
        "note": NOTE,
    }
    return output, (0 if admitted["apply_check"] == "passed" else 1)
