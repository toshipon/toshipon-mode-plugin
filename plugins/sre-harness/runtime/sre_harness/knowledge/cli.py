"""`sre-harness knowledge <stage>`: the knowledge index. This PR has `status` and `update`."""

import json

from .. import paths
from ..errors import EXIT_OK
from . import adapters, ingest, store


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


def register(groups):
    knowledge = groups.add_parser(
        "knowledge", help="knowledge index: approved norms by scope and access (status, update)"
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
