"""`knowledge update`: read a source through its adapter, check every item, hand the rest to
the store in one transaction. No model is called; the same input and config give the same
index.

An item is listed in `rejected`, and kept out of the index, when it

    schema_invalid      does not match knowledge-record `$defs/item`
    access_missing      has no `access` (scope is not access: nothing is readable by default)
    unknown_reader      names a reader no principal in knowledge/config.json holds
    invariant_violated  breaks model.problems (approval, mandatory, scope ids)

Items from different origins with the same content hash are listed in `duplicates` and both
indexed; nothing is merged automatically.
"""

from .. import records
from ..errors import InputError
from . import config, model, store


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


def check(items, known_readers, now):
    """(accepted records, rejected entries) for the items of one update."""
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
        problems = records.violations("knowledge-record", item, "item")
        if problems:
            rejected.append(_rejection(index, origin, "schema_invalid", "; ".join(problems[:5])))
            continue
        if "access" not in item:
            detail = "the item has no access block, so no principal could read it"
            rejected.append(_rejection(index, origin, "access_missing", detail))
            continue
        record = model.normalize(item, now)
        unknown = sorted(set(record["access"]["readers"]) - known_readers)
        if unknown:
            detail = "readers not held by any principal: " + ", ".join(unknown)
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


def update(root, adapter, *, dry_run=False, now=None):
    """Bring the index in line with `adapter`'s source. With `dry_run`, only report."""
    store.require_fts5()
    settings = config.load(root)
    if settings is None:
        raise InputError(
            "knowledge update needs knowledge/config.json under the state root: the principals "
            "in it decide which readers a record may name."
        )
    now = now or records.iso()
    previous = None
    if store.exists(root):
        with store.reader(root) as reader:
            previous = reader.cursor(adapter.source)
    changes = adapter.changes(previous)
    accepted, rejected = check(changes.items, config.known_readers(settings), now)
    batch = store.Batch(
        source=adapter.source,
        adapter=adapter.name,
        cursor=changes.cursor,
        complete=changes.complete,
        records=accepted,
        tombstones=_tombstones(changes, accepted),
        rejected=sorted({entry["id"] for entry in rejected if entry["id"] is not None}),
        config_hash=config.config_hash(settings),
    )
    if dry_run:
        summary = store.plan(root, batch)
    else:
        with store.writer(root) as writer:
            summary = writer.apply(batch, now)
    found = duplicates(accepted)
    return {
        "source": batch.source,
        "dry_run": dry_run,
        "complete": batch.complete,
        "cursor": batch.cursor,
        "generation": summary["generation"],
        "advanced": summary["advanced"],
        "config_changed": summary["config_changed"],
        "counts": {
            "items": len(changes.items),
            "indexed": len(accepted),
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
