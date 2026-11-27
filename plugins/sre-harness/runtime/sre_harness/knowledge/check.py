"""`knowledge check`: the deterministic part of a self-review. The approved declarative checks
that apply to a task are applied to a patch with the interpreter `review evaluate` uses
(`review.apply_rules`); nothing else judges anything. No model, no network.

    1. the task (`task-context`) and the patch are external input: the patch is capped at
       MAX_PATCH_BYTES, must be UTF-8 without control characters (a tab and a CR before the
       line end aside), and every path it names is relative and never climbs (exit 3)
    2. resolve the principal (unknown or no config: exit 3); an index built with another
       config stops with stale_index
    3. the records that apply are `search.narrow`'s: the same scope, facets, replacement and
       access as `knowledge search`, with the task's changed paths and the patch's paths
    4. a policy_check record from the review registry is applied with the check parsed from
       its artifact, after the artifact's sha256 is the one the record and the version record
       both hold and the version record's own copy agrees with it (`candidates.content`, as
       `registry.active_rules` does). Anything else about it is `unfetched` with a reason and
       the check is not applied: source_unavailable, too_large, hash_mismatch,
       content_mismatch, not_a_check
    5. every other record (a prose rule, a decision, a requirement) is `not_checked` with the
       reason `prose`: an agent in another context reads those; this command does not
    6. a violation is a record, a path and the new-side line of an added line its check flags
       (and, with an absent_pattern, a hunk that lacks it), with the record's enforcement

The output holds ids, paths, line numbers and counts, never a line of the patch or of a source,
so it is not counted against the task total. Exit 1 with a violation, an unfetched check, a
check that failed to run or stale_index; 0 otherwise.
"""

import json
import re
from dataclasses import dataclass

from .. import records
from ..errors import EXIT_OK, EXIT_QUALITY, BoundaryError, InputError
from ..review import apply_rules, candidates
from . import config, context, search, sources, store, untrusted

MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PATH_LENGTH = 400
POLICY_CHECK = "policy_check"
PROSE = "prose"
REGISTRY_ORIGIN = "review-registry"
SOURCE_UNAVAILABLE = sources.SOURCE_UNAVAILABLE
_HUNK = re.compile(r"@@ -([0-9]+)(?:,([0-9]+))? \+([0-9]+)(?:,([0-9]+))? @@")
_CONTROL = re.compile(r"[\x00-\x08\x0a-\x1f\x7f-\x9f]")  # a tab is text; "\n" splits lines


@dataclass(frozen=True)
class Hunk:
    path: str
    text: str  # the header and the body, as apply_rules.sides reads a hunk
    added_lines: tuple  # the new-side line number of each added line, in order


@dataclass(frozen=True)
class Patch:
    hunks: tuple
    paths: tuple  # every file the patch changes, deleted files aside
    sha256: str


# The patch -------------------------------------------------------------------------------


def _path(raw, prefix):
    """The path a `---` / `+++` line names, or None for /dev/null."""
    value = raw.split("\t")[0]
    if value == "/dev/null":
        return None
    if value.startswith('"'):
        raise InputError("the patch names a quoted path; only plain paths are read.")
    if value.startswith(prefix):
        value = value[len(prefix) :]
    if (
        not value
        or len(value) > MAX_PATH_LENGTH
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(untrusted.invisible(character) for character in value)
    ):
        raise BoundaryError("the patch names a path outside the repository; nothing was checked.")
    return value


def _lines(data):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise InputError("the patch is not UTF-8 text.") from None
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()  # the end of the last line, not an empty line
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    for number, line in enumerate(lines, start=1):
        if _CONTROL.search(line):
            raise InputError(f"line {number} of the patch holds a control character.")
    return lines


class _Reader:
    """A unified diff, hunk by hunk. Inside a hunk the counts of its header decide where it
    ends, so an added line that looks like a file header is still an added line."""

    def __init__(self):
        self.hunks, self.paths = [], set()
        self.path, self.header = None, False
        self.old_left = self.new_left = 0
        self.body, self.added, self.next_line = [], [], 0

    def _close(self):
        if self.path is not None:
            self.hunks.append(Hunk(self.path, "\n".join(self.body), tuple(self.added)))

    def _inside(self, number, line):
        marker = line[:1]
        if marker == "\\":  # "\ No newline at end of file"
            self.body.append(line)
            return
        if marker == "+":
            self.new_left -= 1
            self.added.append(self.next_line)
            self.next_line += 1
        elif marker == "-":
            self.old_left -= 1
        elif marker == " " or line == "":
            self.old_left -= 1
            self.new_left -= 1
            self.next_line += 1
        else:
            raise InputError(f"line {number} of the patch ends a hunk before its header's count.")
        if self.old_left < 0 or self.new_left < 0:
            raise InputError(f"line {number} of the patch runs past its hunk's count.")
        self.body.append(line)
        if not (self.old_left or self.new_left):
            self._close()

    def _start(self, number, match):
        if not self.header:
            raise InputError(f"line {number} of the patch starts a hunk with no file header.")
        self.old_left = int(match[2]) if match[2] is not None else 1
        self.new_left = int(match[4]) if match[4] is not None else 1
        self.next_line = int(match[3])
        self.body, self.added = [match[0]], []
        if not (self.old_left or self.new_left):
            self._close()

    def feed(self, number, line):
        if self.old_left > 0 or self.new_left > 0:
            self._inside(number, line)
        elif line.startswith("diff --git "):
            self.path, self.header = None, False
        elif line.startswith("--- "):
            _path(line[4:], "a/")
        elif line.startswith("+++ "):
            self.path, self.header = _path(line[4:], "b/"), True
            if self.path is not None:
                self.paths.add(self.path)
        else:
            match = _HUNK.match(line)
            if match:
                self._start(number, match)
            # anything else between hunks (index, mode, rename lines, "\ No newline") is
            # a header line the check has no use for

    def finish(self):
        if self.old_left or self.new_left:
            raise InputError("the patch ends inside a hunk.")


def parse(data):
    """The hunks of a unified diff (bytes, already capped). Exit 2 for a malformed patch,
    exit 3 for a path that leaves the repository."""
    reader = _Reader()
    for number, line in enumerate(_lines(data), start=1):
        reader.feed(number, line)
    reader.finish()
    return Patch(tuple(reader.hunks), tuple(sorted(reader.paths)), records.sha256(data))


def read(path):
    return parse(context.read_capped(path, MAX_PATCH_BYTES, "patch"))


# The checks ------------------------------------------------------------------------------


def resolve(root, record):
    """(check, None) parsed from the artifact of a registry policy_check record, or (None, the
    reason it cannot be applied)."""
    pointers = [
        (evidence, sources.registry_artifact(evidence["locator"]))
        for evidence in record["evidence"]
    ]
    pointers = [(evidence, named) for evidence, named in pointers if named is not None]
    if record["origin"]["kind"] != REGISTRY_ORIGIN or len(pointers) != 1:
        return None, SOURCE_UNAVAILABLE
    evidence, (entry, version_name) = pointers[0]
    found = sources.registry_version(root, entry, version_name)
    if found.text is None:
        return None, found.reason
    try:
        version = json.loads(found.text)
    except json.JSONDecodeError:
        return None, SOURCE_UNAVAILABLE
    if not isinstance(version, dict) or records.violations("review-registry", version):
        return None, SOURCE_UNAVAILABLE
    block = version["candidate"]
    if (
        entry != record["origin"]["key"]
        or version["id"] != entry
        or block["artifact_file"] != f"{version_name}.md"
    ):
        return None, SOURCE_UNAVAILABLE
    artifact = sources.read(root, record, evidence)
    if artifact.text is None:
        return None, artifact.reason
    digest = records.sha256(artifact.text)
    if digest != evidence["content_sha256"] or digest != block["content_sha256"]:
        return None, "hash_mismatch"
    stored = {"id": entry, **{k: block[k] for k in ("form", "title", "check") if k in block}}
    try:
        parsed = candidates.content(stored, artifact.text)
    except BoundaryError:
        return None, "content_mismatch"
    if "check" not in parsed:
        return None, "not_a_check"
    return parsed["check"], None


def _kind(check):
    if check.get("absent_pattern"):
        return "added_line_pattern_and_absent_pattern"
    return "added_line_pattern"


def violations(rules, patch, env=None):
    """({rule id: [(hunk, line)]}, {rule id: skipped long lines}, {rule id: failure}).

    Two passes through apply_rules.apply: hunks first (the absent_pattern spares a whole
    hunk), then the added lines of the flagged hunks alone with the added_line_pattern, to
    name the lines."""
    units = []
    for number, hunk in enumerate(patch.hunks):
        added, new = apply_rules.sides(hunk.text)
        units.append({"id": f"H{number}", "path": hunk.path, "added": added, "new": new})
    results, failures = apply_rules.apply(rules, units, env=env)
    found = {rule_id: [] for rule_id in results}
    skipped = {rule_id: result["skipped_long_lines"] for rule_id, result in results.items()}
    flagged = {rule_id: set(result["units"]) for rule_id, result in results.items()}
    lines = []
    for number, hunk in enumerate(patch.hunks):
        if not any(f"H{number}" in hit for hit in flagged.values()):
            continue
        added, _ = apply_rules.sides(hunk.text)
        for position, (text, line) in enumerate(zip(added, hunk.added_lines)):
            unit = f"H{number}:{position}"
            lines.append(
                {"id": unit, "path": hunk.path, "added": [text], "new": [text], "at": line}
            )
    by_unit = {unit["id"]: unit for unit in lines}
    narrowed = [
        {
            "id": rule["id"],
            "check": {k: v for k, v in rule["check"].items() if k != "absent_pattern"},
        }
        for rule in rules
        if flagged.get(rule["id"])
    ]
    named, more_failures = apply_rules.apply(narrowed, lines, env=env) if narrowed else ({}, {})
    failures.update(more_failures)
    for rule_id, hunks in flagged.items():
        if rule_id in more_failures:
            continue
        hits = [u for u in named.get(rule_id, {"units": []})["units"] if u.split(":")[0] in hunks]
        for hunk_id in sorted(hunks, key=lambda name: int(name[1:])):
            at = [by_unit[u]["at"] for u in hits if u.split(":")[0] == hunk_id]
            if not at:  # flagged, yet no single line names it: point at the hunk
                hunk = patch.hunks[int(hunk_id[1:])]
                at = [hunk.added_lines[0]] if hunk.added_lines else []
            found[rule_id] += [(patch.hunks[int(hunk_id[1:])], line) for line in at]
    return found, skipped, failures


# The command -----------------------------------------------------------------------------


def _empty(status):
    return {
        "status": status,
        "applied": [],
        "violations": [],
        "counts": {"mandatory": 0, "advisory": 0},
        "not_checked": [],
        "unfetched": [],
        "failed": [],
    }


def run(root, task, patch, env=None):
    """Steps 2-6 for one task and one parsed patch."""
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    principal = config.principal(settings, task["principal"])
    with store.reader(root) as reader:
        meta = reader.meta()
        head = {
            "schema_version": records.SCHEMA_VERSION,
            "snapshot": {"generation": reader.generation, "updated_at": meta["updated_at"]},
            "principal": principal.name,
            "task_id": task["task_id"],
            "patch": {
                "files": len(patch.paths),
                "hunks": len(patch.hunks),
                "sha256": patch.sha256,
            },
        }
        if meta["config_hash"] != config.config_hash(settings):
            return {**head, **_empty("stale_index")}
        rows = reader.records(principal)
    live = search.narrow(rows, context.request(task, paths=patch.paths)).live
    body = _empty("ok")
    rules, enforcement = [], {}
    for row in live:
        if row["kind"] != POLICY_CHECK:
            body["not_checked"].append({"id": row["id"], "reason": PROSE})
            continue
        check, problem = resolve(root, row)
        if check is None:
            body["unfetched"].append({"id": row["id"], "reason": problem})
            continue
        rules.append({"id": row["id"], "check": check})
        enforcement[row["id"]] = row["enforcement"]
    found, skipped, failures = violations(rules, patch, env) if rules else ({}, {}, {})
    body["failed"] = [
        {"id": rule_id, "reason": reason.split(":")[0]}
        for rule_id, reason in sorted(failures.items())
    ]
    checks = {rule["id"]: rule["check"] for rule in rules}
    for rule in rules:
        if rule["id"] in failures:
            continue
        body["applied"].append(
            {
                "id": rule["id"],
                "enforcement": enforcement[rule["id"]],
                "skipped_long_lines": skipped.get(rule["id"], 0),
            }
        )
        for hunk, line in found.get(rule["id"], []):
            body["violations"].append(
                {
                    "id": rule["id"],
                    "enforcement": enforcement[rule["id"]],
                    "path": hunk.path,
                    "line": line,
                    "check": _kind(checks[rule["id"]]),
                }
            )
    body["violations"].sort(key=lambda row: (row["path"], row["line"], row["id"]))
    for row in body["violations"]:
        body["counts"][row["enforcement"]] += 1
    if body["violations"]:
        body["status"] = "violations"
    elif body["unfetched"] or body["failed"]:
        body["status"] = "incomplete"
    return {**head, **body}


def exit_code(result):
    return EXIT_OK if result["status"] == "ok" else EXIT_QUALITY
