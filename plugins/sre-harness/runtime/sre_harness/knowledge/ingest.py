"""`knowledge update`: read a source through its adapter, check every item, hand the rest to
the store in one transaction. No model is called; the same input and config give the same
index.

Before anything of the source is read, `admit` refuses (exit 3) an adapter of SYNTHETIC_ONLY in
a state whose config is not marked `"state_kind": "synthetic"`.

An item is listed in `rejected`, and kept out of the index, when it (`detail` names JSON paths
and the checks they fail, never a value of the input)

    schema_invalid      does not match knowledge-record `$defs/item`
    access_missing      has no `access` (scope is not access: nothing is readable by default)
    unknown_reader      names a reader no principal in knowledge/config.json holds
    invariant_violated  breaks model.problems (approval, mandatory, scope ids)
    invalid_glob        has a path glob that could make matching slow (globs.problem)
    origin_kind_reserved  has an origin kind that only another adapter may give
                        (RESERVED_KINDS: a file cannot pose as the review registry or a run)
    owned_by_other_source  has the id of a record another source holds (the store's plan);
                        that record is left as it is

Items from different origins with the same content hash are listed in `duplicates` and both
indexed; nothing is merged automatically.
"""

from dataclasses import dataclass

from .. import globs, records
from ..errors import BoundaryError, InputError
from . import config, model, store

# origin kinds only one adapter gives: a record of these kinds comes from its approval flow
RESERVED_KINDS = {
    "review-registry": "review-registry",
    "decision": "spec-run",
    "requirement": "spec-run",
}
OWNED_ELSEWHERE = "owned_by_other_source"
# adapters whose input declares status, approval, enforcement and access itself, with no
# approval flow behind it: only a state marked synthetic takes them in
SYNTHETIC_ONLY = frozenset({"file"})


def _origin(item):
    origin = item.get("origin") if isinstance(item, dict) else None
    if records.violations("knowledge-record", origin, "origin"):
        return None
    return origin


def _rejection(index, origin, reason, detail):
    return {
        "index": index,
        "origin": None if origin is None else {"kind": origin["kind"], "key": origin["key"]},
        "id": None if origin is None else model.record_id(origin["kind"], origin["key"]),
        "reason": reason,
        "detail": detail,
    }


def check(items, known_readers, now, adapter=None):
    """(accepted records, rejected entries) for the items of one update from `adapter` (its
    name; RESERVED_KINDS are refused from any other)."""
    seen = {}
    for index, item in enumerate(items):
        origin = _origin(item)
        if origin is None:
            continue
        key = (origin["kind"], origin["key"])
        if key in seen:
            raise InputError(
                f"items {seen[key]} and {index} have the same origin {origin['kind']}:"
                f"{origin['key']}; an origin is one record and cannot appear twice. "
                "Nothing was written."
            )
        seen[key] = index
    accepted, rejected = [], []
    for index, item in enumerate(items):
        origin = _origin(item)
        problems = records.violation_paths("knowledge-record", item, "item")
        if problems:
            rejected.append(_rejection(index, origin, "schema_invalid", "; ".join(problems[:5])))
            continue
        if any(globs.problem(glob) for glob in item.get("facets", {}).get("path_globs", ())):
            detail = (
                f"a path glob is longer than {globs.MAX_GLOB_LENGTH} characters or has more "
                f"than {globs.MAX_DOUBLE_STARS} '**'"
            )
            rejected.append(_rejection(index, origin, globs.INVALID, detail))
            continue
        owner = RESERVED_KINDS.get(item["origin"]["kind"])
        if owner is not None and owner != adapter:
            detail = f"this origin kind is given only by the {owner} source"
            rejected.append(_rejection(index, origin, "origin_kind_reserved", detail))
            continue
        if "access" not in item:
            detail = "the item has no access block, so no principal could read it"
            rejected.append(_rejection(index, origin, "access_missing", detail))
            continue
        record = model.normalize(item, now)
        unknown = [
            f"access/readers/{position}"
            for position, reader in enumerate(item["access"]["readers"])
            if reader not in known_readers
        ]
        if unknown:
            detail = "readers not held by any principal at " + ", ".join(unknown)
            rejected.append(_rejection(index, origin, "unknown_reader", detail))
            continue
        broken = model.problems(record)
        if broken:
            rejected.append(_rejection(index, origin, "invariant_violated", "; ".join(broken)))
            continue
        accepted.append(record)
    return accepted, rejected


def duplicates(accepted):
    groups = {}
    for record in accepted:
        groups.setdefault(record["content_hash"], []).append(record["id"])
    return [
        {"content_hash": digest, "ids": sorted(ids)}
        for digest, ids in sorted(groups.items())
        if len(ids) > 1
    ]


def _tombstones(changes, accepted):
    stones = []
    kept = {record["id"] for record in accepted}
    for stone in changes.tombstones:
        origin = {"kind": stone["origin"]["kind"], "key": stone["origin"]["key"]}
        record_id = model.record_id(origin["kind"], origin["key"])
        if record_id in kept:
            raise InputError(
                f"{origin['kind']}:{origin['key']} is both an item and a tombstone. "
                "Nothing was written."
            )
        stones.append({"id": record_id, "origin": origin, "reason": stone["reason"]})
    return stones


@dataclass(frozen=True)
class Prepared:
    """One source's answer, checked and turned into the batch a writer applies."""

    changes: object  # adapters.Changes
    accepted: list
    rejected: list
    batch: store.Batch


def settings(root):
    """The knowledge config an update needs, or exit 2."""
    found = config.load(root)
    if found is None:
        raise InputError(
            "knowledge update needs knowledge/config.json under the state root: the principals "
            "in it decide which readers a record may name."
        )
    return found


def admit(adapter, found):
    """Exit 3 unless this state may take `adapter` in. Nothing of the source is read."""
    if adapter.name in SYNTHETIC_ONLY and not config.synthetic(found):
        raise BoundaryError(
            f"refused: --source {adapter.name} is for synthetic data only, and "
            'knowledge/config.json does not say "state_kind": "synthetic". Its input declares '
            "approval, enforcement and access itself. In a production state, take knowledge in "
            "through an approval flow: `review approve` (with --knowledge-* for scope, access "
            "and enforcement), then `knowledge update --source review-registry`; or "
            "`knowledge update --source spec-run`."
        )


def prepare(adapter, found, previous, now):
    """Ask `adapter` for its changes since the cursor `previous` and check every item."""
    changes = adapter.changes(previous)
    accepted, rejected = check(changes.items, config.known_readers(found), now, adapter.name)
    left_out = {entry["id"] for entry in changes.skipped or () if entry["id"] is not None}
    batch = store.Batch(
        source=adapter.source,
        adapter=adapter.name,
        cursor=changes.cursor,
        complete=changes.complete,
        records=accepted,
        tombstones=_tombstones(changes, accepted),
        rejected=sorted({entry["id"] for entry in rejected if entry["id"] is not None} | left_out),
        config_hash=config.config_hash(found),
    )
    return Prepared(changes, accepted, rejected, batch)


def _held_elsewhere(items, held):
    """The rejected entries of the items whose ids another source holds."""
    found = []
    for index, item in enumerate(items):
        origin = _origin(item)
        if origin is not None and model.record_id(origin["kind"], origin["key"]) in held:
            detail = "another source holds this record id; the item was not written"
            found.append(_rejection(index, origin, OWNED_ELSEWHERE, detail))
    return found


def report(prepared, summary, dry_run):
    """The update's answer, from what was prepared and what the store did (or would do)."""
    batch, accepted = prepared.batch, prepared.accepted
    held = set(summary["owned_by_other_source"])
    rejected = sorted(
        prepared.rejected + _held_elsewhere(prepared.changes.items, held),
        key=lambda entry: entry["index"],
    )
    found = duplicates(accepted)
    answer = {
        "source": batch.source,
        "dry_run": dry_run,
        "complete": batch.complete,
        "cursor": batch.cursor,
        "generation": summary["generation"],
        "advanced": summary["advanced"],
        "config_changed": summary["config_changed"],
        "counts": {
            "items": len(prepared.changes.items),
            "indexed": len(accepted) - len(held),
            "inserted": len(summary["inserted"]),
            "updated": len(summary["updated"]),
            "unchanged": summary["unchanged"],
            "tombstoned": len(summary["tombstoned"]),
            "rejected": len(rejected),
            "duplicates": len(found),
        },
        "tombstoned": summary["tombstoned"],
        "rejected": rejected,
        "duplicates": found,
    }
    if prepared.changes.skipped is not None:
        answer["skipped"] = prepared.changes.skipped
    return answer


def update(root, adapter, *, dry_run=False, now=None):
    """Bring the index in line with `adapter`'s source. With `dry_run`, only report.

    The source is read under the writer lock, so a writer that holds it (a `volume import`
    replacing the registry, another update) is never read half way; a dry run takes no lock
    and reads the source as it is."""
    store.require_fts5()
    found = settings(root)
    admit(adapter, found)
    now = now or records.iso()
    if dry_run:
        previous = None
        if store.exists(root):
            with store.reader(root) as reader:
                previous = reader.cursor(adapter.source)
        prepared = prepare(adapter, found, previous, now)
        return report(prepared, store.plan(root, prepared.batch), dry_run)
    with store.locked(root) as held:
        previous = None
        if store.exists(root):
            with store.reader(root) as reader:
                previous = reader.cursor(adapter.source)
        prepared = prepare(adapter, found, previous, now)
        with held.writer() as writer:
            summary = writer.apply(prepared.batch, now)
    return report(prepared, summary, dry_run)
