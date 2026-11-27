"""The `review-registry` adapter: the decided candidates of `review approve / reject / retire`
(`registry/RG-*/v*.json` under the state root) as knowledge items. It only reads the registry;
no file there is ever written. No model is called.

One entry is one record, at the entry's latest version:

    origin       {kind: review-registry, key: RG-<n>, version: <latest>}
    kind         policy_check for a declarative check, rule for prose
    status       the latest version's: approved, rejected or retired
    summary      the title parsed from the artifact (`candidates.parse`; 200 characters at most)
    facets       repo from the entry, path_globs from the declarative check
    evidence     registry:RG-<n>/v<n>.md at the latest version, its sha256 and size
    approval     the decision of the latest approved version (none for an entry never approved)
    updated_at   when the latest version was recorded

Scope, access, enforcement, applies_when and exceptions come from `norms`: the `knowledge`
block a person gave `review approve` when there is one, otherwise the defaults (the project
whose `repos` in knowledge/config.json hold the entry's repo, at project level, that project's
`default_access`, and `advisory`). Only such a block makes a record mandatory, and only while
the entry's latest version is approved.

An entry is listed in `skipped`, and yields no item, when it is

    unmapped  repo_not_in_config        no project of the config holds its repo
              repo_in_several_projects  more than one does, so its scope is not known
    invalid   artifact_unreadable       its artifact cannot be read
              hash_mismatch             the artifact changed after it was recorded
              content_mismatch          the record's form, title or check differ from the
                                        artifact (as `registry.active_rules` refuses it)

The registry is read whole every time, so the answer is `complete`: an entry that is no longer
an item (skipped now, or gone) leaves the index. The cursor is the sha256 of {entry: latest
version}; the store works out what changed from the records themselves.
"""

from ... import records
from ...errors import BoundaryError, InputError
from ...review import candidates, registry
from .. import config, model
from ..block import placement

NAME = "review-registry"
KINDS = {"declarative_check": "policy_check", "prose": "rule"}
APPROVER_LENGTH = 100  # knowledge-record $defs/approval approver
UNMAPPED = "unmapped"
INVALID = "invalid"


def norms(versions, project_id, project):
    """Scope, access, enforcement, applies_when and exceptions of one entry ({versions}, oldest
    first). The one place that decides them.

    The `knowledge` block that places the entry (`block.placement`: the latest version's when
    it is approved, else the latest approved version's) gives all five, as the person approved
    them. Mandatory holds only while the latest version is approved: a retired or rejected
    entry is advisory whatever a block says. Without a block, the defaults: `project_id`'s
    project level, its `default_access`, advisory, no applies_when or exceptions."""
    placed = placement(versions)
    if placed is not None:
        approved = versions[-1]["status"] == "approved"
        return {
            "scope": dict(placed["scope"]),
            "access": {
                "classification": placed["access"]["classification"],
                "readers": list(placed["access"]["readers"]),
            },
            "enforcement": placed["enforcement"] if approved else "advisory",
            "applies_when": list(placed["applies_when"]),
            "exceptions": list(placed["exceptions"]),
        }
    return {
        "scope": {
            "level": "project",
            "project_id": project_id,
            "component_id": None,
            "task_id": None,
        },
        "access": {
            "classification": project["default_access"]["classification"],
            "readers": list(project["default_access"]["readers"]),
        },
        "enforcement": "advisory",
        "applies_when": [],
        "exceptions": [],
    }


def artifact(root, latest):
    """(text, None) of the latest version's artifact, as `registry.active_rules` checks it, or
    (None, the reason it cannot be used)."""
    try:
        text = registry.artifact(root, latest)
    except InputError:
        return None, "artifact_unreadable"
    if records.sha256(text) != latest["candidate"]["content_sha256"]:
        return None, "hash_mismatch"
    stored = {
        k: latest["candidate"][k] for k in ("form", "title", "check") if k in latest["candidate"]
    }
    try:
        candidates.content({"id": latest["id"], **stored}, text)
    except BoundaryError:
        return None, "content_mismatch"
    return text, None


def approval(versions):
    """The decision of the latest approved version, as a knowledge approval record."""
    approved = [version for version in versions if version["status"] == "approved"]
    if not approved:
        return None
    decision = approved[-1]["decision"]
    return {
        "approver": decision["reviewer"][:APPROVER_LENGTH],
        "decided_at": decision["decided_at"],
        "via": decision["via"],
    }


def item(latest, versions, text, decided):
    """The knowledge item of one entry whose artifact `text` was checked."""
    block = latest["candidate"]
    check = block.get("check") or {}
    return {
        "origin": {"kind": NAME, "key": latest["id"], "version": latest["version"]},
        "kind": KINDS[block["form"]],
        "scope": decided["scope"],
        "facets": {"repo": latest["repo"], "path_globs": list(check.get("path_globs", []))},
        "access": decided["access"],
        "summary": " ".join(block["title"].split()),
        "applies_when": decided["applies_when"],
        "exceptions": decided["exceptions"],
        "status": latest["status"],
        "enforcement": decided["enforcement"],
        "confidence": None,
        "evidence": [
            {
                "locator": f"registry:{latest['id']}/{block['artifact_file']}",
                "revision": block["artifact_file"].removesuffix(".md"),
                "content_sha256": block["content_sha256"],
                "size": len(text.encode("utf-8")),
            }
        ],
        "updated_at": latest["created_at"],
        "approval": approval(versions),
    }


def skipped(latest, kind, reason):
    return {
        "entry": latest["id"],
        "version": latest["version"],
        "id": model.record_id(NAME, latest["id"]),
        "repo": latest["repo"],
        "status": latest["status"],
        "kind": kind,
        "reason": reason,
    }


class ReviewRegistryAdapter:
    name = NAME
    source = NAME

    def __init__(self, root):
        self.root = root

    @classmethod
    def from_options(cls, options):
        from . import refuse_run_options

        refuse_run_options(cls.name, options)
        if options.get("input"):
            raise InputError(
                "--source review-registry reads registry/ under the state root; it takes no "
                "--input."
            )
        return cls(options["root"])

    def changes(self, cursor):
        from . import Changes

        settings = config.load(self.root)
        if settings is None:
            raise InputError("the review-registry source needs knowledge/config.json.")
        found = registry.entries(self.root)
        items, left_out = [], []
        for versions in found.values():
            latest = versions[-1]
            project_id, project = config.project_of(settings, latest["repo"])
            if project_id is None:
                left_out.append(skipped(latest, UNMAPPED, project))
                continue
            text, problem = artifact(self.root, latest)
            if text is None:
                left_out.append(skipped(latest, INVALID, problem))
                continue
            items.append(item(latest, versions, text, norms(versions, project_id, project)))
        versions = {entry_id: rows[-1]["version"] for entry_id, rows in found.items()}
        return Changes(
            items=items,
            tombstones=[],
            cursor="sha256:" + records.fingerprint(versions),
            complete=True,
            skipped=left_out,
        )
