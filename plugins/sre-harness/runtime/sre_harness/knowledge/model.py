"""Knowledge records as plain data: ids, hashes, normalization and the invariants. Pure.

id            KN- + the first 12 hex of sha256(canonical([origin.kind, origin.key]))
content_hash  fingerprint of the normative fields (NORMATIVE)
relations     trace relations to other records ({relation, target id}), sorted; only present
              when there are some
record_hash   fingerprint of every field but indexed_at; decides whether a write is needed
"""

import re

from .. import records

PREFIX = "KN-"
ID = re.compile(r"KN-[0-9a-f]{12}")
NORMATIVE = ("summary", "applies_when", "exceptions", "status", "enforcement", "scope", "access")
LEVELS = ("organization", "project", "component", "task")
SCOPE_IDS = ("project_id", "component_id", "task_id")  # the id each level below organization adds
NEEDS_APPROVAL = frozenset({"approved", "superseded"})
FACET_LISTS = ("topic", "resource_type", "path_globs")


def record_id(kind, key):
    return PREFIX + records.sha256(records.canonical([kind, key]))[:12]


def content_hash(record):
    return records.fingerprint({name: record[name] for name in NORMATIVE})


def record_hash(record):
    return records.fingerprint({k: v for k, v in record.items() if k != "indexed_at"})


def _sorted_unique(values):
    return sorted(set(values))


def normalize(item, indexed_at):
    """A record from a schema-valid item that has `access`. Sets are sorted so that the same
    item always gives the same hashes; sentence lists keep their order."""
    facets = item.get("facets", {})
    record = {
        "schema_version": records.SCHEMA_VERSION,
        "id": record_id(item["origin"]["kind"], item["origin"]["key"]),
        "origin": dict(item["origin"]),
        "kind": item["kind"],
        "scope": dict(item["scope"]),
        "facets": {
            **{name: _sorted_unique(facets.get(name, [])) for name in FACET_LISTS},
            "repo": facets.get("repo"),
            "decision_id": facets.get("decision_id"),
        },
        "access": {
            "classification": item["access"]["classification"],
            "readers": _sorted_unique(item["access"]["readers"]),
        },
        "summary": item["summary"],
        "applies_when": list(item.get("applies_when", [])),
        "exceptions": list(item.get("exceptions", [])),
        "status": item["status"],
        "enforcement": item["enforcement"],
        "confidence": item.get("confidence"),
        "evidence": [dict(evidence) for evidence in item["evidence"]],
        "supersedes": _sorted_unique(
            record_id(ref["kind"], ref["key"]) for ref in item.get("supersedes", [])
        ),
        "updated_at": item["updated_at"],
        "indexed_at": indexed_at,
        "approval": item.get("approval"),
    }
    relations = sorted(
        {
            (row["relation"], record_id(row["target"]["kind"], row["target"]["key"]))
            for row in item.get("relations", [])
        }
    )
    if relations:  # absent means none, so a record of a source without relations is unchanged
        record["relations"] = [{"relation": name, "target": target} for name, target in relations]
    record["content_hash"] = content_hash(record)
    return record


def problems(record):
    """Invariant violations of one record, empty when it holds."""
    found = []
    if record["status"] in NEEDS_APPROVAL and not record.get("approval"):
        found.append(f"a {record['status']} record needs an approval record")
    if record["enforcement"] == "mandatory" and record["status"] != "approved":
        found.append(f"only an approved record can be mandatory, not a {record['status']} one")
    scope = record["scope"]
    depth = LEVELS.index(scope["level"])
    for position, name in enumerate(SCOPE_IDS, start=1):
        if position <= depth and scope[name] is None:
            found.append(f"scope {scope['level']} needs {name}")
        if position > depth and scope[name] is not None:
            found.append(f"scope {scope['level']} must leave {name} null")
    return found


def supersession_cycle(edges):
    """A cycle in `edges` (id -> ids it supersedes) as [a, b, ..., a], or None. Edges to ids
    outside `edges` lead nowhere and cannot close a cycle."""
    state = {}  # id -> 1 on the current path, 2 done
    for start in sorted(edges):
        if state.get(start):
            continue
        path, stack = [], [(start, iter(sorted(edges[start])))]
        state[start] = 1
        path.append(start)
        while stack:
            node, targets = stack[-1]
            target = next(targets, None)
            if target is None:
                stack.pop()
                path.pop()
                state[node] = 2
            elif state.get(target) == 1:
                return path[path.index(target) :] + [target]
            elif target in edges and not state.get(target):
                state[target] = 1
                path.append(target)
                stack.append((target, iter(sorted(edges[target]))))
    return None
