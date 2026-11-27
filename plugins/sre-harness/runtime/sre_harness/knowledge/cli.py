"""`sre-harness knowledge <stage>`: the knowledge index (`status`, `update`, `search`,
`fetch`, `context`, `check`, `decision`, `export`, `migrate`, `rollback`). With `--task`, search and fetch also add what they
return to the task total (`budget`), checked before anything is printed; `context` always does,
for the task its task file names."""

import argparse
import json
import platform
import re
import sys

from .. import paths, privacy, records
from ..errors import EXIT_OK, EXIT_QUALITY, BoundaryError, InputError
from . import (
    adapters,
    budget,
    check,
    config,
    context,
    decisions,
    export,
    fetch,
    ingest,
    migrate,
    model,
    search,
    store,
    untrusted,
)

DECISION_ID_LENGTH = 64
REASON_LENGTH = 200
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")  # knowledge-record $defs/name
_HEAD = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")

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
        "python_version": platform.python_version(),
        "sqlite_version": store.sqlite_version(),
        "fts5": store.fts5_available(),
        "extension_loading": store.extension_loading(),
        "updated_at": None,
        "sources": [],
        "records": {},
    }
    if store.exists(root):
        settings = config.load(root)
        current = None if settings is None else config.config_hash(settings)
        with store.reader(root) as reader:
            meta = reader.meta()
            value.update(
                index="present",
                generation=meta["generation"],
                schema_version=meta["schema_version"],
                updated_at=meta["updated_at"],
                sources=[_followed(row, current) for row in reader.sources()],
                records=reader.status_counts(),
            )
    print(json.dumps(value, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def _followed(row, current):
    """A source as status shows it: whether it was last taken in under the config in place
    (None without a config), not the config's hash."""
    shown = {name: value for name, value in row.items() if name != "config_hash"}
    shown["config_current"] = None if current is None else row["config_hash"] == current
    return shown


def _update(args, env, out):
    root = paths.state_root(env)
    options = {"input": args.input, "root": root, "run": args.run, "project": args.project}
    adapter = adapters.build(args.source, options)
    result = ingest.update(root, adapter, dry_run=args.dry_run)
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def _task(args, task_id):
    """The task to count against, or None without a task. Every check runs before anything is
    read or written. A reason is never echoed: it is the caller's text."""
    extend, reason = args.extend_task_budget, args.reason
    if extend is None and reason is None:
        return budget.Task(task_id) if task_id else None
    if not task_id:
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
    return budget.Task(task_id, extend, reason)


def _charge(root, task, principal, result, *, command, ids, now):
    """The task total's answer for a result about to be printed, or None without --task."""
    if task is None:
        return None
    return budget.charge(
        root,
        task,
        principal,
        command=command,
        ids=ids,
        tokens=result["budget"]["used_tokens"],
        generation=result["snapshot"]["generation"],
        now=now,
    )


def _expansion(args):
    return None if args.expand is None else search.expansion(args.expand)


def _search(args, env, out):
    if args.component and not args.project:
        raise InputError("--component needs --project: a component belongs to one project.")
    task = _task(args, args.task)
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
        expand=_expansion(args),
    )
    return _searched(env, args.principal, request, task, out)


def _context(args, env, out):
    """One search, as the task file describes it (the contract `task-context`)."""
    found = context.load(args.task_file)
    task = _task(args, found["task_id"])
    request = context.request(
        found, query=args.query or "", max_tokens=args.max_tokens, expand=_expansion(args)
    )
    return _searched(env, found["principal"], request, task, out)


def _check(args, env, out):
    """The approved declarative checks of the task, applied to the patch (`knowledge.check`)."""
    found = context.load(args.task_file)
    patch = check.read(args.patch)
    result = check.run(paths.state_root(env), found, patch, env=env)
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return check.exit_code(result)


def _print(value, out):
    print(json.dumps(value, indent=2, ensure_ascii=False), file=out)


def _decision_put(args, env, out):
    result = decisions.put(
        paths.state_root(env),
        context.load(args.task_file),
        question=args.question,
        options=args.option,
        evidence_ids=args.evidence,
        risk=args.risk,
        now=clock(),
        head_sha=args.head_sha,
        expires_at=args.expires_at,
    )
    _print(result, out)
    return EXIT_OK


def _is_terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _decision_answer(args, env, out):
    # As for `review approve`: a person at a terminal, with no flag or variable that skips it.
    # A pty passes; it proves neither that a person is there nor who they are.
    if not (_is_terminal(sys.stdin) and _is_terminal(out)):
        raise BoundaryError(
            "knowledge decision answer needs a terminal on stdin and stdout; it does not run "
            "from a pipe or a non-interactive shell. Nothing was recorded."
        )
    root, now = paths.state_root(env), clock()
    response = decisions.answer(
        root,
        args.request,
        option=args.option,
        responder=args.responder,
        reuse=decisions.reuse_of(args.reuse_scope, args.reuse_until, now),
        now=now,
        ask=input,
        show=lambda text: print(text, file=out),
        env=env,
    )
    _print(response, out)
    return EXIT_OK


def _decision_poll(args, env, out):
    result = decisions.poll(
        paths.state_root(env),
        args.workspace,
        now=clock(),
        task_id=args.task,
        head_sha=args.head_sha,
    )
    _print(result, out)
    return EXIT_OK if result["status"] == "ok" else EXIT_QUALITY


def _decision_list(args, env, out):
    _print(decisions.listing(paths.state_root(env), clock(), args.status), out)
    return EXIT_OK


def _decision_cancel(args, env, out):
    _print(decisions.cancel(paths.state_root(env), args.request, args.reason, clock()), out)
    return EXIT_OK


def _searched(env, principal, request, task, out):
    """Run one search, count it against `task` when there is one, and print it."""
    root, now = paths.state_root(env), clock()
    result = search.run(root, principal, request, now)
    returned = [shown["id"] for shown in result["records"] + result["proposed"]]
    charged = _charge(root, task, principal, result, command="search", ids=returned, now=now)
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
    task = _task(args, args.task)
    request = fetch.Request(
        record_id=args.id,
        max_tokens=args.max_tokens,
        offset=args.offset,
        generation=args.generation,
        deny_terms=tuple(privacy.load_deny_terms(args.deny_terms_file)),
    )
    root, now = paths.state_root(env), clock()
    result = fetch.run(root, args.principal, request)
    charged = _charge(root, task, args.principal, result, command="fetch", ids=[args.id], now=now)
    if charged is not None:
        if not charged.allowed:
            result = fetch.exhausted(result, charged.view)
        result["budget"]["task"] = charged.view
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return fetch.exit_code(result)


def _export(args, env, out):
    """The readable approved records as project context explorer entities (stdout only)."""
    _print(export.run(paths.state_root(env), args.principal, args.project), out)
    return EXIT_OK


def _migrate(args, env, out):
    root, now = paths.state_root(env), clock()
    result = migrate.plan(root, now) if args.dry_run else migrate.run(root, now)
    print(json.dumps(result, indent=2, ensure_ascii=False), file=out)
    return migrate.exit_code(result)


def _rollback(args, env, out):
    root = paths.state_root(env)
    if args.list:
        value = {"backups": migrate.backups(root)}
    else:
        value = migrate.rollback(root, args.to, clock())
    print(json.dumps(value, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


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
    _expand_argument(found)
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


def _expand_argument(parser):
    parser.add_argument(
        "--expand",
        action="append",
        metavar="WORD",
        help=f"a word the caller adds to the query (repeat, at most {search.MAX_EXPAND_WORDS} of "
        f"{search.MAX_EXPAND_LENGTH} characters); advisory records only, never mandatory",
    )


def _task_budget_arguments(parser):
    parser.add_argument(
        "--extend-task-budget",
        type=_count(1),
        metavar="N",
        help="raise the task total by N tokens (needs --task and --reason; recorded)",
    )
    parser.add_argument("--reason", help="why the task total is extended (one line, recorded)")


def _register_context(stages):
    found = stages.add_parser(
        "context", help="the search a task file (task-context) describes, counted to its task"
    )
    found.add_argument("--task-file", required=True, help="a task-context JSON file (only read)")
    found.add_argument("--query", help="words to rank the advisory records by (never mandatory)")
    _expand_argument(found)
    found.add_argument("--max-tokens", type=_count(1), default=search.DEFAULT_MAX_TOKENS)
    _task_budget_arguments(found)
    found.set_defaults(handler=_context)


def _register_check(stages):
    checked = stages.add_parser(
        "check", help="the task's approved declarative checks, applied to a unified diff"
    )
    checked.add_argument("--task-file", required=True, help="a task-context JSON file (only read)")
    checked.add_argument("--patch", required=True, help="a unified diff file (only read)")
    checked.set_defaults(handler=_check)


def _pattern(compiled, what):
    def parse(text):
        if not compiled.fullmatch(text):
            raise argparse.ArgumentTypeError(f"needs {what}")
        return text

    return parse


def _register_decision(stages):
    decision = stages.add_parser(
        "decision", help="the reference decision queue: files under knowledge/decisions/"
    )
    steps = decision.add_subparsers(dest="step", required=True)
    request_id = _pattern(decisions.REQUEST_ID, "DR- and 12 lower-case hex digits")
    head = _pattern(_HEAD, "40 or 64 lower-case hex digits")

    put = steps.add_parser("put", help="ask a person; accepted, or a duplicate of the same ask")
    put.add_argument("--task-file", required=True, help="a task-context JSON file (only read)")
    put.add_argument("--question", required=True, help="one line, the question for a person")
    put.add_argument("--option", action="append", required=True, help="repeat: 2-10 options")
    put.add_argument(
        "--evidence", action="append", required=True, type=_record_id, help="repeat: KN-..."
    )
    put.add_argument("--risk", required=True, choices=decisions.RISKS)
    put.add_argument("--head-sha", type=head, help="the head the question is about")
    put.add_argument("--expires-at", help="a UTC time after which the request takes no answer")
    put.set_defaults(handler=_decision_put)

    answering = steps.add_parser("answer", help="a person's answer, at a terminal, recorded once")
    answering.add_argument("--request", required=True, type=request_id)
    answering.add_argument("--option", required=True, help="one of the request's options")
    answering.add_argument("--responder", required=True, help="the person who answers")
    answering.add_argument("--reuse-scope", choices=decisions.REUSE_SCOPES, default="task")
    answering.add_argument("--reuse-until", help="until when (a UTC time); project needs it")
    answering.set_defaults(handler=_decision_answer)

    polling = steps.add_parser("poll", help="a workspace's requests and its answers, once each")
    polling.add_argument("--workspace", required=True, type=_name)
    polling.add_argument("--task", type=_name, help="only the requests of this task")
    polling.add_argument(
        "--head-sha", type=head, help="the head now; an answer for another is stale"
    )
    polling.set_defaults(handler=_decision_poll)

    listing = steps.add_parser("list", help="every request with its status now")
    listing.add_argument(
        "--status", choices=("open", "answered", "cancelled", "expired", "superseded")
    )
    listing.set_defaults(handler=_decision_list)

    cancelling = steps.add_parser("cancel", help="withdraw a request that is not answered")
    cancelling.add_argument("--request", required=True, type=request_id)
    cancelling.add_argument("--reason", required=True, help="one line, recorded")
    cancelling.set_defaults(handler=_decision_cancel)


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
        help="knowledge index: approved norms by scope and access (status, update, search, fetch, "
        "context, check, decision, export, migrate, rollback)",
    )
    stages = knowledge.add_subparsers(dest="stage", required=True)

    status = stages.add_parser(
        "status", help="generation, Python, SQLite, FTS5 and extension loading, sources, counts"
    )
    status.set_defaults(handler=_status)

    update = stages.add_parser("update", help="read a source into the index (one transaction)")
    update.add_argument("--source", required=True, choices=sorted(adapters.ADAPTERS))
    update.add_argument("--input", help="the JSON file the file source reads (file only)")
    update.add_argument("--run", help="the spec run whose registries are read (spec-run only)")
    update.add_argument(
        "--project", help="the project of knowledge/config.json they belong to (spec-run only)"
    )
    update.add_argument(
        "--dry-run", action="store_true", help="report what would change and write nothing"
    )
    update.set_defaults(handler=_update)

    _register_search(stages)
    _register_fetch(stages)
    _register_context(stages)
    _register_check(stages)
    _register_decision(stages)

    exporting = stages.add_parser(
        "export", help="the approved records a principal reads, for another tool (stdout only)"
    )
    exporting.add_argument("--principal", required=True, help="who reads (knowledge/config.json)")
    exporting.add_argument(
        "--format",
        required=True,
        choices=(export.FORMAT,),
        help="context-entities: the entities project-context-explorer's `index add` reads",
    )
    exporting.add_argument("--project", type=_name, help="the organization and this project only")
    exporting.set_defaults(handler=_export)

    migrating = stages.add_parser(
        "migrate",
        help="take the review registry in after a backup, checked against it before the swap",
    )
    migrating.add_argument("--dry-run", action="store_true", help="list the plan and write nothing")
    migrating.set_defaults(handler=_migrate)

    rolling = stages.add_parser("rollback", help="put a backup of the index back, or list them")
    chosen = rolling.add_mutually_exclusive_group(required=True)
    chosen.add_argument("--to", metavar="BACKUP", help="a name from --list")
    chosen.add_argument("--list", action="store_true", help="the backups, oldest first")
    rolling.set_defaults(handler=_rollback)
