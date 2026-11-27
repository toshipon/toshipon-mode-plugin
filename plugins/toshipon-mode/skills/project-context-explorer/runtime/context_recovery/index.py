"""The metadata index: what exists, where its source is and how things relate. It holds no
document bodies. Descriptions are one short line; the source is fetched when a question needs it."""

import re
from pathlib import Path

from .base import SCHEMA_VERSION, Refusal, now, read_json, sha256
from .claims import check_reference, cited_text, parse_locator
from .gitread import checked_path, head, show
from .redaction import redact
from .untrusted import clean

TYPES = (
    "objective",
    "component",
    "resource",
    "property",
    "requirement",
    "decision",
    "change",
    "meeting_note",
    "document",
    "risk",
    "question",
)
# refines / implements / verifies / depends_on / conflicts_with are the relations of the
# sre-harness trace (trace-link.schema.json); the rest name structure and history.
RELATIONS = (
    "refines",
    "implements",
    "verifies",
    "depends_on",
    "conflicts_with",
    "part_of",
    "supersedes",
    "decided_by",
    "discussed_in",
    "changed_by",
)
# The statuses of the sre-harness Decision Registry (decision.schema.json).
DECISION_STATUSES = ("proposed", "approved", "rejected", "superseded", "unresolved")
SOURCE_CATEGORIES = ("git", "pull_requests", "documents", "meeting_notes", "chat", "tickets")
RATIONALE_RELATIONS = frozenset({"decided_by", "discussed_in", "implements", "refines"})
MAX_DESCRIPTION = 240
_ID = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,79}$")
_ENTITY_KEYS = {
    "id",
    "type",
    "description",
    "config_path",
    "source",
    "relationships",
    "status",
    "rationale_recorded",
}


def empty(root):
    return {
        "schema_version": SCHEMA_VERSION,
        "root": str(Path(root).resolve()),
        "updated_at": now(),
        "entities": {},
        "sources": [],
    }


def load(path, root):
    path = Path(path)
    if not path.exists():
        return empty(root)
    index = read_json(path, "index")
    if index.get("schema_version") != SCHEMA_VERSION:
        raise Refusal(f"index schema_version is not {SCHEMA_VERSION}: {path}")
    return index


def check_entity(entity):
    """Problems with one entity as it arrives from outside. Empty when it can be stored."""
    if not isinstance(entity, dict):
        return ["entity must be an object"]
    name = entity.get("id")
    if not isinstance(name, str) or not _ID.match(name):
        return [f"entity id must match {_ID.pattern}: {name!r}"]
    problems = [f"{name}: unknown field {key}" for key in sorted(set(entity) - _ENTITY_KEYS)]
    if entity.get("type") not in TYPES:
        problems.append(f"{name}: type must be one of {', '.join(TYPES)}")
    description = entity.get("description")
    if not isinstance(description, str) or not description.strip():
        problems.append(f"{name}: description is empty")
    elif len(description) > MAX_DESCRIPTION or "\n" in description.strip():
        problems.append(
            f"{name}: description must be one line of at most {MAX_DESCRIPTION} characters;"
            " the index holds metadata, not document bodies"
        )
    if "config_path" in entity:
        try:
            checked_path(entity["config_path"])
        except Refusal as refused:
            problems.append(f"{name}: config_path: {refused}")
    source = entity.get("source")
    if source is not None:
        if not isinstance(source, dict) or set(source) - {"locator", "revision", "sha256", "size"}:
            problems.append(f"{name}: source must be {{locator, revision?, sha256?, size?}}")
        elif "revision" in source:
            problems += [f"{name}: {problem}" for problem in check_reference(source)]
        else:
            try:
                parse_locator(source.get("locator"))
            except Refusal as refused:
                problems.append(f"{name}: {refused}")
    relationships = entity.get("relationships", [])
    if not isinstance(relationships, list):
        problems.append(f"{name}: relationships must be a list")
        relationships = []
    for relation in relationships:
        if (
            not isinstance(relation, dict)
            or set(relation) != {"type", "to"}
            or relation["type"] not in RELATIONS
            or not isinstance(relation["to"], str)
            or not _ID.match(relation["to"])
        ):
            problems.append(
                f"{name}: relationship must be {{type: {' | '.join(RELATIONS)}, to: <id>}}"
            )
    if "status" in entity and entity["status"] not in DECISION_STATUSES:
        problems.append(f"{name}: status must be one of {', '.join(DECISION_STATUSES)}")
    if "rationale_recorded" in entity and not isinstance(entity["rationale_recorded"], bool):
        problems.append(f"{name}: rationale_recorded must be true or false")
    return problems


def stamp(entity, root, revision):
    """Fill the revision, hash and size of a git source that came without a revision.
    The hash covers the cited lines, so a change elsewhere in the file does not make it stale."""
    source = entity.get("source")
    if not source or "revision" in source or parse_locator(source["locator"])["kind"] != "git":
        return entity
    where = parse_locator(source["locator"])
    text = show(root, revision, where["path"])
    cited = None if text is None else cited_text(text, where)
    if cited is None:
        raise Refusal(f"{entity['id']}: {source['locator']} is not in the repository at {revision}")
    size = len(cited.encode("utf-8"))
    return {
        **entity,
        "source": {**source, "revision": revision, "sha256": sha256(cited), "size": size},
    }


def upsert(index, entities, root):
    """Add or replace entities. All of them are checked before any is stored."""
    if not isinstance(entities, list):
        raise Refusal("entities must be a list")
    problems = [problem for entity in entities for problem in check_entity(entity)]
    if problems:
        raise Refusal("entities do not follow the contract", problems)
    needs_stamp = any(e.get("source") and "revision" not in e["source"] for e in entities)
    revision = head(root) if needs_stamp else None
    for entity in entities:
        entity = stamp(entity, root, revision)
        entity = redact({**entity, "description": " ".join(clean(entity["description"]).split())})
        entity.setdefault("relationships", [])
        index["entities"][entity["id"]] = entity
    index["updated_at"] = now()
    return index


def set_sources(index, sources):
    """What the session found it can read. An absent category is a recorded gap, not a guess."""
    problems = []
    for source in sources if isinstance(sources, list) else [None]:
        if (
            not isinstance(source, dict)
            or source.get("category") not in SOURCE_CATEGORIES
            or not isinstance(source.get("available"), bool)
            or not isinstance(source.get("tool", ""), str)
        ):
            problems.append(
                f"source must be {{category: {' | '.join(SOURCE_CATEGORIES)}, available: bool,"
                f" tool?: str, note?: str}}: {source!r}"
            )
    if problems:
        raise Refusal("sources do not follow the contract", problems)
    index["sources"] = redact(
        [
            {
                "category": source["category"],
                "available": source["available"],
                "tool": " ".join(clean(source.get("tool", "")).split())[:80],
                "note": " ".join(clean(str(source.get("note", ""))).split())[:MAX_DESCRIPTION],
            }
            for source in sources
        ]
    )
    index["updated_at"] = now()
    return index


def _short(text):
    text = " ".join(clean(text or "").split())
    return text if len(text) <= MAX_DESCRIPTION else text[: MAX_DESCRIPTION - 1] + "…"


def _registry_source(record):
    """The first piece of evidence of a registry record that names a file at a commit."""
    for evidence in record.get("evidence", []):
        match = re.match(r"^([0-9a-f]{40}):([^#]+)(?:#L([0-9]+))?", evidence.get("ref", ""))
        if match:
            line = f"#L{match[3]}" if match[3] else ""
            try:
                checked_path(match[2])
            except Refusal:
                continue
            return {"locator": f"git:{match[2]}{line}", "revision": match[1]}
    return None


def from_registry(decisions=None, requirements=None, trace=None):
    """Entities from the sre-harness registries (decisions.json, requirements.json, trace.json).
    The registries stay the source of truth for decisions and requirements; this only indexes them."""
    try:
        given = [value for value in (decisions, requirements, trace) if value is not None]
        if not all(isinstance(value, dict) for value in given):
            raise TypeError("a registry file is an object")
        return _from_registry(decisions or {}, requirements or {}, trace or {})
    except (KeyError, TypeError, AttributeError) as wrong:
        raise Refusal(
            f"registry file is not in the shape sre-harness writes: {type(wrong).__name__}: {wrong}"
        ) from None


def _from_registry(decisions, requirements, trace):
    entities = {}
    for decision in decisions.get("decisions", []):
        relations = [{"type": "supersedes", "to": old} for old in decision.get("supersedes", [])]
        entity = {
            "id": decision["id"],
            "type": "decision",
            "description": _short(decision.get("title") or decision.get("content"))
            or decision["id"],
            "status": decision["status"],
            "rationale_recorded": bool(decision.get("rationale", "").strip()),
            "relationships": relations,
        }
        source = _registry_source(decision)
        if source:
            entity["source"] = source
        entities[entity["id"]] = entity
    for requirement in requirements.get("requirements", []):
        entity = {
            "id": requirement["id"],
            "type": "requirement",
            "description": _short(requirement.get("title") or requirement.get("statement"))
            or requirement["id"],
            "rationale_recorded": bool(requirement.get("rationale", "").strip()),
            "relationships": [
                {"type": "decided_by", "to": ref} for ref in requirement.get("decision_refs", [])
            ],
        }
        source = _registry_source(requirement)
        if source:
            entity["source"] = source
        entities[entity["id"]] = entity
    for link in trace.get("links", []):
        ends = (link["from"], link["to"])
        if all(
            end["type"] in ("decision", "requirement") and end["id"] in entities for end in ends
        ):
            relation = {"type": link["relation"], "to": link["to"]["id"]}
            if relation not in entities[link["from"]["id"]]["relationships"]:
                entities[link["from"]["id"]]["relationships"].append(relation)
    return list(entities.values())


def edges(index):
    """(from, relation, to) for every relationship whose target is in the index."""
    return [
        (entity["id"], relation["type"], relation["to"])
        for entity in index["entities"].values()
        for relation in entity["relationships"]
        if relation["to"] in index["entities"]
    ]


def check(index, root):
    """Gaps, stale references and conflicts, found by rule. Nothing here is inferred: a
    missing rationale is reported as missing, never filled in from the configuration."""
    findings = []
    entities = index["entities"]
    revision = head(root)
    not_checked = 0
    for entity in entities.values():
        name = entity["id"]
        for relation in entity["relationships"]:
            if relation["to"] not in entities:
                findings.append(
                    {
                        "kind": "dangling_reference",
                        "entity": name,
                        "detail": f"{relation['type']} -> {relation['to']}",
                    }
                )
            elif relation["type"] == "conflicts_with":
                other = entities[relation["to"]]
                both = entity.get("status") == other.get("status") == "approved"
                findings.append(
                    {
                        "kind": "conflict",
                        "entity": name,
                        "detail": f"conflicts_with {other['id']}"
                        + (" (both approved, neither supersedes the other)" if both else ""),
                    }
                )
        kinds = {relation["type"] for relation in entity["relationships"]}
        if entity.get("rationale_recorded") is False or (
            entity["type"] in ("property", "resource")
            and "rationale_recorded" not in entity
            and not kinds & RATIONALE_RELATIONS
        ):
            findings.append(
                {
                    "kind": "missing_rationale",
                    "entity": name,
                    "detail": "no recorded source says why; do not infer it from the configuration",
                }
            )
        source = entity.get("source")
        if not source:
            findings.append(
                {"kind": "no_source", "entity": name, "detail": "no source locator recorded"}
            )
            continue
        where = parse_locator(source["locator"])
        if where["kind"] != "git":
            not_checked += 1
            continue
        recorded = source.get("sha256")
        if recorded is None:  # a stated revision without a hash: read what it cited back then
            then = show(root, source["revision"], where["path"])
            cited = None if then is None else cited_text(then, where)
            if cited is None:
                not_checked += 1
                continue
            recorded = sha256(cited)
        text = show(root, revision, where["path"])
        current = None if text is None else cited_text(text, where)
        if current is None:
            findings.append(
                {
                    "kind": "missing_source",
                    "entity": name,
                    "detail": f"{source['locator']} is not at {revision[:12]}",
                }
            )
        elif sha256(current) != recorded:
            findings.append(
                {
                    "kind": "stale_reference",
                    "entity": name,
                    "detail": f"{source['locator']} changed since {source['revision'][:12]} (now {revision[:12]})",
                }
            )
    for source in index["sources"]:
        if not source["available"]:
            findings.append(
                {
                    "kind": "source_unavailable",
                    "entity": None,
                    "detail": f"{source['category']}: not reachable from this session",
                }
            )
    return {"head": revision, "findings": findings, "not_checked": not_checked}
