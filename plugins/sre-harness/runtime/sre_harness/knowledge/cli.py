"""`sre-harness knowledge <stage>`: the knowledge index (`status`, `update`, `search`,
`fetch`). With `--task`, search and fetch also add what they return to the task total
(`budget`), checked before anything is printed."""

import argparse
import json
import re

from .. import paths, privacy, records
from ..errors import EXIT_OK, InputError
from . import adapters, budget, fetch, ingest, model, search, store, untrusted

DECISION_ID_LENGTH = 64
REASON_LENGTH = 200
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # knowledge-record $defs/name

# The clock `search` measures freshness with and the task total stamps calls with; tests
# replace it.
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


def _task(args):
    """The task to count against, or None without --task. Every check runs before anything is
    read or written. A reason is never echoed: it is the caller's text."""
    extend, reason = args.extend_task_budget, args.reason
    if extend is None and reason is None:
        return budget.Task(args.task) if args.task else None
    if not args.task:
        raise InputError("--extend-task-budget and --reason extend a task total: give --task.")
    if extend is None or reason is None:
        raise InputError("--extend-task-budget needs --reason, and --reason needs the extension.")
    if (
        not 1 <= len(reason) <= REASON_LENGTH
        or reason != reason.strip()
        or any(untrusted.invisible(character) for character in reason)
    ):
        raise InputError(
            f"--reason must be one line of 1-{REASON_LENGTH} characters without control characters."
        )
    if privacy.find_secrets(reason):
        raise InputError("--reason has the shape of a secret; it was not recorded.")
    return budget.Task(args.task, extend, reason)


def _charge(root, task, args, result, *, command, ids, now):
    """The task total's answer for a result about to be printed, or None without --task."""
    if task is None:
        return None
    return budget.charge(
        root,
        task,
        args.principal,
        command=command,
        ids=ids,
        tokens=result["budget"]["used_tokens"],
        generation=result["snapshot"]["generation"],
        now=now,
    )


def _search(args, env, out):
    if args.component and not args.project:
        raise InputError("--component needs --project: a component belongs to one project.")
    task = _task(args)
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
    root, now = paths.state_root(env), clock()
    result = search.run(root, args.principal, request, now)
    returned = [shown["id"] for shown in result["records"] + result["proposed"]]
    charged = _charge(root, task, args, result, command="search", ids=returned, now=now)
    if charged is not None:
        if not charged.allowed:
            result = {
                **result,
                **search.stopped("task_budget_exhausted", request, result["mandatory"]),
            }
        result["budget"]["task"] = charged.view
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return search.exit_code(result)


def _fetch(args, env, out):
    task = _task(args)
    request = fetch.Request(
        record_id=args.id,
        max_tokens=args.max_tokens,
        offset=args.offset,
        generation=args.generation,
        deny_terms=tuple(privacy.load_deny_terms(args.deny_terms_file)),
    )
    root, now = paths.state_root(env), clock()
    result = fetch.run(root, args.principal, request)
    charged = _charge(root, task, args, result, command="fetch", ids=[args.id], now=now)
    if charged is not None:
        if not charged.allowed:
            result = fetch.exhausted(result, charged.view)
        result["budget"]["task"] = charged.view
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return fetch.exit_code(result)


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


def _name(text):
    if not _NAME.fullmatch(text):
        raise argparse.ArgumentTypeError("needs 1-64 letters, digits, '.', '_' or '-'")
    return text


def _record_id(text):
    if not model.ID.fullmatch(text):
        raise argparse.ArgumentTypeError("needs KN- and 12 lower-case hex digits")
    return text


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
    found.add_argument(
        "--task", type=_name, help="adds the records scoped to this task, and counts the total"
    )
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
    _task_budget_arguments(found)
    found.set_defaults(handler=_search)


def _task_budget_arguments(parser):
    parser.add_argument(
        "--extend-task-budget",
        type=_count(1),
        metavar="N",
        help="raise the task total by N tokens (needs --task and --reason; recorded)",
    )
    parser.add_argument("--reason", help="why the task total is extended (one line, recorded)")


def _register_fetch(stages):
    fetched = stages.add_parser(
        "fetch", help="the source text behind one record's evidence, a page at a time"
    )
    fetched.add_argument("--principal", required=True, help="who fetches (knowledge/config.json)")
    fetched.add_argument("--id", required=True, type=_record_id, help="the record (KN-...)")
    fetched.add_argument("--task", type=_name, help="count what is returned to this task's total")
    fetched.add_argument("--max-tokens", type=_count(1), default=fetch.DEFAULT_MAX_TOKENS)
    fetched.add_argument("--offset", type=_count(0), default=0, help="the next_offset of a page")
    fetched.add_argument(
        "--generation", type=_count(0), help="the generation the search saw; a move is reported"
    )
    fetched.add_argument(
        "--deny-terms-file", help="terms that keep a source back (withheld, never printed)"
    )
    _task_budget_arguments(fetched)
    fetched.set_defaults(handler=_fetch)


def register(groups):
    knowledge = groups.add_parser(
        "knowledge",
        help="knowledge index: approved norms by scope and access (status, update, search, fetch)",
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
    _register_fetch(stages)
