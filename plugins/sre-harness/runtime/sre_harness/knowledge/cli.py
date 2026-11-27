"""`sre-harness knowledge <stage>`: the knowledge index (`status`, `update`, `search`)."""

import argparse
import json

from .. import paths, records
from ..errors import EXIT_OK, InputError
from . import adapters, ingest, search, store

DECISION_ID_LENGTH = 64

# The clock `search` measures freshness with; tests replace it.
clock = records.iso


def _status(args, env, out):
    """Operator view: no principal, so no project, reader or record content is printed."""
    root = paths.state_root(env)
    value = {
        "index": "absent",
        "generation": 0,
        "schema_version": store.SCHEMA_VERSION,
        "sqlite_version": store.sqlite_version(),
        "fts5": store.fts5_available(),
        "updated_at": None,
        "sources": [],
        "records": {},
    }
    if store.exists(root):
        with store.reader(root) as reader:
            meta = reader.meta()
            value.update(
                index="present",
                generation=meta["generation"],
                schema_version=meta["schema_version"],
                updated_at=meta["updated_at"],
                sources=reader.sources(),
                records=reader.status_counts(),
            )
    print(json.dumps(value, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def _update(args, env, out):
    adapter = adapters.build(args.source, {"input": args.input})
    root = paths.state_root(env)
    result = ingest.update(root, adapter, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def _search(args, env, out):
    if args.component and not args.project:
        raise InputError("--component needs --project: a component belongs to one project.")
    request = search.Request(
        project=args.project,
        component=args.component,
        task=args.task,
        query=args.query or "",
        repo=args.repo,
        paths=tuple(args.path or ()),
        resource_types=tuple(args.resource_type or ()),
        topics=tuple(args.topic or ()),
        decision_id=args.decision_id,
        include_proposed=args.include_proposed,
        max_tokens=args.max_tokens,
        stale_after_hours=args.stale_after_hours,
    )
    result = search.run(paths.state_root(env), args.principal, request, clock())
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return search.exit_code(result)


def _count(minimum):
    def parse(text):
        try:
            value = int(text)
        except ValueError:
            value = None
        if value is None or value < minimum:
            raise argparse.ArgumentTypeError(f"needs a whole number of at least {minimum}")
        return value

    return parse


def _decision_id(text):
    if not 1 <= len(text) <= DECISION_ID_LENGTH:
        raise argparse.ArgumentTypeError(f"needs 1-{DECISION_ID_LENGTH} characters")
    return text


def _register_search(stages):
    found = stages.add_parser(
        "search", help="the approved norms for one piece of work, within a token budget"
    )
    found.add_argument("--principal", required=True, help="who searches (knowledge/config.json)")
    found.add_argument("--project")
    found.add_argument("--component", help="needs --project")
    found.add_argument("--task", help="adds the records scoped to this task")
    found.add_argument("--query", help="words to rank the advisory records by (never mandatory)")
    found.add_argument("--repo")
    found.add_argument("--path", action="append", help="a changed path; repeat for more")
    found.add_argument("--resource-type", action="append", help="repeat for more")
    found.add_argument("--topic", action="append", help="narrows the advisory records")
    found.add_argument("--decision-id", type=_decision_id, help="one decision, or its successor")
    found.add_argument(
        "--include-proposed", action="store_true", help="also return proposed records, apart"
    )
    found.add_argument("--max-tokens", type=_count(1), default=search.DEFAULT_MAX_TOKENS)
    found.add_argument("--stale-after-hours", type=_count(0), default=search.STALE_AFTER_HOURS)
    found.set_defaults(handler=_search)


def register(groups):
    knowledge = groups.add_parser(
        "knowledge",
        help="knowledge index: approved norms by scope and access (status, update, search)",
    )
    stages = knowledge.add_subparsers(dest="stage", required=True)

    status = stages.add_parser("status", help="generation, SQLite and FTS5, sources, counts")
    status.set_defaults(handler=_status)

    update = stages.add_parser("update", help="read a source into the index (one transaction)")
    update.add_argument("--source", required=True, choices=sorted(adapters.ADAPTERS))
    update.add_argument("--input", help="the JSON file the file source reads")
    update.add_argument(
        "--dry-run", action="store_true", help="report what would change and write nothing"
    )
    update.set_defaults(handler=_update)

    _register_search(stages)
