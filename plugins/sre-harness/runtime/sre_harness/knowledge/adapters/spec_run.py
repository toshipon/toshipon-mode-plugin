"""The `spec-run` adapter: the Decision and Requirement Registries of one spec run
(`runs/<run id>/decisions.json`, `requirements.json`, and `trace.json` when `spec analyze` has
written it) as knowledge items of one project. It only reads the run. No model is called.

    knowledge update --source spec-run --run <run id> --project <project id>

The key is the id a document writes, never the id a run assigns (`DEC-0001` is numbered per
run): a decision is `origin {kind: decision, key: <project>:<external_id>}`, an explicit
requirement `{kind: requirement, key: <project>:<REQ id>}` (its `REQ-` id is the one its
document writes). The same decision taken from another run is the same record.

Left out, and listed in `skipped` with the reason:

    decision     no_external_id   no document id, so no id that holds across runs
                 inferred         proposed by a model; a person has not written it down
    requirement  inferred         an `RQI-` requirement, for the same reason
    relation     inferred         an inferred trace link is never an edge of the index

What an item holds:

    status       a decision's registry status (proposed, approved, rejected, superseded,
                 unresolved). A requirement has none in its registry and nobody approved it
                 here: proposed
    enforcement  advisory, always. A document that says "approved" is a document, not a
                 person's decision in this harness; only `review approve` makes a rule mandatory
    approval     for approved and superseded decisions, the registry's approvers (names),
                 `decided_at` (the day, at 00:00 UTC; the run's time when the document gives
                 none) and `via: spec-document`
    supersedes   the registry's (run ids mapped to the records of their external ids)
    relations    explicit trace links between two records of this run (`refines`,
                 `implements`, `verifies`, `depends_on`, `conflicts_with`), on the record the
                 link starts from
    evidence     the registry's evidence sections as `run:<run id>/documents.jsonl#DOC#S`,
                 hashed over the section text, so `knowledge fetch` reads them back
    scope        the project, at project level, with its `default_access`

A registry that breaks the invariants of `spec.registry.check` (a supersession cycle among
them) is not taken in: exit 2 and nothing is written. The source is `spec-run:<project id>`,
and every update is complete: the run given is the registry's latest state, so a decision only
an older run held leaves the index. The cursor is the run id and the sha256 of its files.
"""

import re

from ... import paths, records
from ...errors import InputError
from ...spec import registry as spec_registry
from .. import config, model

NAME = "spec-run"
DECISION, REQUIREMENT = "decision", "requirement"
VIA = "spec-document"
SUMMARY_LENGTH = 240  # knowledge-record $defs/summary
APPROVER_LENGTH = 100  # knowledge-record $defs/approval approver
FILES = ("decisions.json", "requirements.json", "trace.json")
_SECTION = re.compile(r"DOC-[0-9]{4,}#S[0-9]{1,6}")  # what a run: locator of fetch names


def _present(run_dir, name):
    path = paths.child(run_dir, name)
    if path.is_symlink():
        raise InputError(f"refused to read {name} of the run through a symlink.")
    return path.is_file()


def _read(run_dir, name, schema, definition):
    """(value, sha256 of the file) of one run file, checked against its schema; exit 2 else."""
    path = paths.child(run_dir, name)
    if not _present(run_dir, name):
        raise InputError(f"the run has no {name}; run `sre-harness spec extract` on it first.")
    data = path.read_bytes()
    value = records.read_json(path)
    problems = records.violations(schema, value, definition)
    if problems:
        raise InputError(f"{name} of the run is invalid: " + "; ".join(problems[:5]))
    return value, records.sha256(data)


def _sections(run_dir):
    """{'DOC-n#Sn': section text} of the run's documents.jsonl (empty when there is none)."""
    path = paths.child(run_dir, "documents.jsonl")
    if path.is_symlink() or not path.is_file():
        return {}
    found = {}
    for row in records.read_jsonl(path):
        for section in row.get("sections") or ():
            if isinstance(section.get("text"), str):
                found[f"{row.get('id')}#{section.get('id')}"] = section["text"]
    return found


def _summary(*texts):
    for text in texts:
        line = " ".join((text or "").split())
        if line:
            return line[:SUMMARY_LENGTH]
    return None


class _Run:
    """One run's registries, read and checked, and how each entry maps onto the index."""

    def __init__(self, root, run_id, project_id):
        settings = config.load(root)
        if settings is None:
            raise InputError("the spec-run source needs knowledge/config.json.")
        self.project = settings["projects"].get(project_id)
        if self.project is None:
            raise InputError(f"project {project_id!r} is not in knowledge/config.json.")
        self.project_id = project_id
        self.run_id = run_id
        run_dir = paths.open_run(root, run_id)
        self.decisions_file, decisions_sha = _read(run_dir, FILES[0], "decision", "registry")
        self.requirements_file, requirements_sha = _read(
            run_dir, FILES[1], "requirement", "registry"
        )
        self.trace_file, trace_sha = {"links": []}, None  # no trace before `spec analyze`
        if _present(run_dir, FILES[2]):
            self.trace_file, trace_sha = _read(run_dir, FILES[2], "trace-link", "file")
        for name, value in zip(FILES, (self.decisions_file, self.requirements_file)):
            if value["run_id"] != run_id:
                raise InputError(f"{name} names run {value['run_id']}, not {run_id}.")
        problems = spec_registry.check(self.decisions_file["decisions"])
        if problems:
            raise InputError(
                "the run's Decision Registry breaks its invariants: "
                + "; ".join(problems[:5])
                + ". Nothing was written."
            )
        self.sections = _sections(run_dir)
        self.digest = records.fingerprint(
            dict(zip(FILES, (decisions_sha, requirements_sha, trace_sha)))
        )
        self.keys = {}  # (node type, registry id) -> origin ref of an indexed entry
        for item in self.decisions_file["decisions"]:
            if item["basis"] == "explicit" and item.get("external_id"):
                self.keys[(DECISION, item["id"])] = self.ref(DECISION, item["external_id"])
        for item in self.requirements_file["requirements"]:
            if item["basis"] == "explicit":
                written = item.get("external_id") or item["id"]
                self.keys[(REQUIREMENT, item["id"])] = self.ref(REQUIREMENT, written)
        self.written = {
            item["external_id"]: item["id"]
            for item in self.decisions_file["decisions"]
            if (DECISION, item["id"]) in self.keys
        }

    def ref(self, kind, written):
        return {"kind": kind, "key": f"{self.project_id}:{written}"}

    def node(self, end):
        """The origin ref of a trace node, or None when it is not an indexed entry. A decision
        node names a registry id, or a written id when the registry lacks it."""
        kind, name = end["type"], end["id"]
        if kind == DECISION and (kind, name) not in self.keys:
            name = self.written.get(name, name)
        return self.keys.get((kind, name))

    def evidence(self, item):
        """The item's evidence as locators fetch reads, or None when a section is missing."""
        out, seen = [], set()
        for entry in item["evidence"]:
            ref = entry["ref"]
            text = self.sections.get(ref) if _SECTION.fullmatch(ref) else None
            if text is None:
                return None
            if ref in seen:
                continue
            seen.add(ref)
            out.append(
                {
                    "locator": f"run:{self.run_id}/documents.jsonl#{ref}",
                    "revision": self.run_id,
                    "content_sha256": records.sha256(text),
                    "size": len(text.encode("utf-8")),
                }
            )
        return out

    def relations(self):
        """({origin key: relations}, skipped rows) from the explicit links of trace.json."""
        found, left_out = {}, []
        for link in self.trace_file["links"]:
            source, target = self.node(link["from"]), self.node(link["to"])
            if source is None or target is None or source == target:
                continue
            if link["basis"] != "explicit":
                left_out.append(
                    {
                        "entry": link["id"],
                        "id": None,
                        "kind": "relation",
                        "status": None,
                        "reason": "inferred",
                    }
                )
                continue
            row = {"relation": link["relation"], "target": target}
            rows = found.setdefault(source["key"], [])
            if row not in rows:
                rows.append(row)
        return found, left_out


def _skipped(entry, kind, ref, status, reason):
    return {
        "entry": entry,
        "id": None if ref is None else model.record_id(ref["kind"], ref["key"]),
        "kind": kind,
        "status": status,
        "reason": reason,
    }


def _approval(item, created_at):
    if item["status"] not in spec_registry.IN_FORCE:
        return None
    names = ", ".join(person["name"] for person in item["approvers"])
    day = item["decided_at"]
    return {
        "approver": names[:APPROVER_LENGTH],
        "decided_at": f"{day}T00:00:00Z" if day else created_at,
        "via": VIA,
    }


def _item(run, ref, kind, entry, summary, status, evidence, relations, written):
    value = {
        "origin": dict(ref),
        "kind": kind,
        "scope": {
            "level": "project",
            "project_id": run.project_id,
            "component_id": None,
            "task_id": None,
        },
        "facets": {"decision_id": written if kind == DECISION else None},
        "access": {
            "classification": run.project["default_access"]["classification"],
            "readers": list(run.project["default_access"]["readers"]),
        },
        "summary": summary,
        "applies_when": [],
        "exceptions": [],
        "status": status,
        "enforcement": "advisory",  # never mandatory from a document (see the module doc)
        "confidence": {"value": entry["confidence"], "vocabulary": "spec"},
        "evidence": evidence,
        "updated_at": run.decisions_file["created_at"],
        "approval": None,
    }
    if relations:
        value["relations"] = relations
    return value


def _decisions(run, relations):
    items, left_out = [], []
    for entry in run.decisions_file["decisions"]:
        ref = run.keys.get((DECISION, entry["id"]))
        if entry["basis"] != "explicit" or ref is None:
            written = entry.get("external_id")
            stated = None if written is None else run.ref(DECISION, written)
            reason = "inferred" if entry["basis"] != "explicit" else "no_external_id"
            left_out.append(_skipped(entry["id"], DECISION, stated, entry["status"], reason))
            continue
        evidence = run.evidence(entry)
        if evidence is None:
            left_out.append(
                _skipped(entry["id"], DECISION, ref, entry["status"], "evidence_unresolved")
            )
            continue
        summary = _summary(entry["title"], entry["content"], entry["external_id"])
        value = _item(
            run,
            ref,
            DECISION,
            entry,
            summary,
            entry["status"],
            evidence,
            relations.get(ref["key"]),
            entry["external_id"],
        )
        value["approval"] = _approval(entry, run.decisions_file["created_at"])
        replaced = [run.keys.get((DECISION, target)) for target in entry["supersedes"]]
        value["supersedes"] = [target for target in replaced if target is not None]
        items.append(value)
    return items, left_out


def _requirements(run, relations):
    items, left_out = [], []
    for entry in run.requirements_file["requirements"]:
        ref = run.keys.get((REQUIREMENT, entry["id"]))
        if ref is None:
            left_out.append(_skipped(entry["id"], REQUIREMENT, None, None, "inferred"))
            continue
        evidence = run.evidence(entry)
        if evidence is None:
            left_out.append(_skipped(entry["id"], REQUIREMENT, ref, None, "evidence_unresolved"))
            continue
        summary = _summary(entry.get("title"), entry["statement"], entry["id"])
        items.append(
            _item(
                run,
                ref,
                REQUIREMENT,
                entry,
                summary,
                "proposed",
                evidence,
                relations.get(ref["key"]),
                None,
            )
        )
    return items, left_out


class SpecRunAdapter:
    name = NAME

    def __init__(self, root, run_id, project_id):
        self.root = root
        self.run_id = run_id
        self.project_id = project_id

    @classmethod
    def from_options(cls, options):
        if options.get("input"):
            raise InputError("--source spec-run reads a run under the state root; no --input.")
        if not options.get("run") or not options.get("project"):
            raise InputError("--source spec-run needs --run <run id> and --project <project id>.")
        return cls(options["root"], options["run"], options["project"])

    @property
    def source(self):
        return f"{NAME}:{self.project_id}"

    def changes(self, cursor):
        from . import Changes

        run = _Run(self.root, self.run_id, self.project_id)
        relations, unlinked = run.relations()
        decisions, left_decisions = _decisions(run, relations)
        requirements, left_requirements = _requirements(run, relations)
        return Changes(
            items=decisions + requirements,
            tombstones=[],
            cursor=f"{self.run_id}:sha256:{run.digest}",
            complete=True,
            skipped=left_decisions + left_requirements + unlinked,
        )
