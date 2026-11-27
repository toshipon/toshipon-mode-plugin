"""`sre-harness review <stage>`."""

import json
import re
import sys
import time
from pathlib import Path

from .. import guard, llm, paths, privacy, records
from ..errors import EXIT_OK, EXIT_QUALITY, BoundaryError, InputError
from ..github.fixture import FixtureTransport
from ..github.rest import GhTransport, Limiter
from ..knowledge import block as knowledge_block
from ..knowledge import config as knowledge_config
from . import analyze as analyze_stage
from . import approve as approve_stage
from . import classify as classify_stage
from . import collect as collect_stage
from . import evaluate as evaluate_stage
from . import generate as generate_stage
from . import normalize as normalize_stage
from . import split as split_stage

# Injected so tests can drive rate-limit waits without waiting.
clock = time.time
sleep = time.sleep
timer = time.monotonic

DEFAULT_MAX_WAIT = 900
_REPO = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/((?!\.{1,2}$)[A-Za-z0-9._-]{1,100})")
_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")


def _checked_period(args):
    if not _REPO.fullmatch(args.repo):
        raise InputError("--repo must be OWNER/NAME.")
    for name in ("since", "until"):
        if not _TIME.fullmatch(getattr(args, name)):
            raise InputError(f"--{name} must be UTC like 2026-01-31T00:00:00Z.")
    if args.since >= args.until:
        raise InputError("--since must be earlier than --until.")
    if args.max_wait < 0:
        raise InputError("--max-wait must not be negative.")


def _collect(args, env, out):
    _checked_period(args)
    if args.fixture is not None:
        transport = FixtureTransport(Path(args.fixture))
        source = {"kind": "fixture"}
    else:
        # The token is read before anything else; an empty variable stops here, unsent.
        token = guard.read_token(args.auth_env, env)
        transport = GhTransport(token, host=args.host, env=env)
        source = {"kind": "github", "auth_env": args.auth_env}
    if args.host is not None:
        source["host"] = args.host
    root = paths.state_root(env)
    run_dir = paths.create_run(root)
    args.run = run_dir.name  # so the stage timing lands in the new run
    limiter = Limiter(clock, sleep, args.max_wait)
    record = collect_stage.collect(
        transport, limiter, run_dir, run_dir.name, args.repo, args.since, args.until, source, clock
    )
    summary = {
        "run_id": record["run_id"],
        "status": record["status"],
        "pulls": len(record["pulls"]),
        "pages": record["page_count"],
        "missing": len(record["missing"]),
        "waited_seconds": record["rate_limit"]["waited_seconds"],
    }
    if "stopped" in record:
        summary["stopped"] = record["stopped"]["reason"]
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK if record["status"] == "complete" else EXIT_QUALITY


def _normalize(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    print(json.dumps(normalize_stage.normalize(run_dir), indent=2), file=out)
    return EXIT_OK


SENDING = (
    "note: --backend claude sends PR bodies, review comments, diffs{rules} to Anthropic "
    "through the Claude CLI (no tools, no MCP servers, auth mode {mode})."
)


def _session(args, run_dir, env, rules=False):
    low, high = guard.CLAUDE_TIMEOUT
    if not low <= args.timeout <= high:
        raise InputError(f"--timeout must be {low}-{high} seconds.")
    deny_terms = privacy.load_deny_terms(args.deny_terms_file)
    session = llm.Session(
        run_dir,
        args.backend,
        auth_mode=args.auth_mode,
        max_run_budget_usd=args.max_run_budget_usd,
        max_call_budget_usd=args.max_call_budget_usd,
        deny_terms=deny_terms,
        timeout=args.timeout,
        model=args.model,
        env=env,
    )
    if args.backend == "claude":
        extra = " and existing rule files" if rules else ""
        print(SENDING.format(rules=extra, mode=args.auth_mode), file=sys.stderr)
    return session


def _split_file(args, run_dir):
    if args.split is None:
        return None
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    return split_stage.load(Path(args.split), collect["repo"])


def _analyze(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    split = _split_file(args, run_dir)
    summary = analyze_stage.analyze(run_dir, _session(args, run_dir, env), split)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _classify(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    session = _session(args, run_dir, env, rules=True)
    summary = classify_stage.classify(run_dir, Path(args.existing_rules), session)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _split(args, env, out):
    if not _TIME.fullmatch(args.t1):
        raise InputError("--t1 must be UTC like 2026-01-31T00:00:00Z.")
    if not 0 <= args.gap_days <= 365:
        raise InputError("--gap-days must be 0-365.")
    run_dir = paths.open_run(paths.state_root(env), args.run)
    made = split_stage.make(run_dir, args.t1, args.gap_days, args.exclude_pr)
    summary = {
        "run_id": run_dir.name,
        "split": str(paths.child(run_dir, split_stage.FILE)),
        "sha256": made["sha256"],
        "generation_prs": len(made["generation"]["prs"]),
        "evaluation_prs": len(made["evaluation"]["prs"]),
        "dropped": made["generation"]["dropped"],
        "excluded": {key: len(value) for key, value in made["excluded"].items()},
    }
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _generate(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    split = _split_file(args, run_dir)
    session = _session(args, run_dir, env)
    summary = generate_stage.generate(run_dir, session, args.include_untrusted, split)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _evaluate(args, env, out):
    root = paths.state_root(env)
    run_dir = paths.open_run(root, args.run)
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    split = split_stage.load(Path(args.split), collect["repo"])
    thresholds = dict(evaluate_stage.metrics.THRESHOLDS)
    if args.seed is not None:
        thresholds["seed"] = args.seed
    if args.resamples is not None:
        if not 100 <= args.resamples <= 100000:
            raise InputError("--resamples must be 100-100000.")
        thresholds["resamples"] = args.resamples
    session = _session(args, run_dir, env)
    evaluation = evaluate_stage.evaluate(
        run_dir, root, split, session, args.adjudication, thresholds
    )
    if args.format == "evaluator-output":
        print(json.dumps(evaluate_stage.evaluator_output(evaluation)), file=out)
    else:
        print(json.dumps(evaluate_stage.summary(evaluation), indent=2), file=out)
    return EXIT_OK


def _is_terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, OSError, ValueError):
        return False


def _require_terminal(command, out):
    """Stop an approval or a retirement run from a pipe or a non-interactive shell.

    It keeps such runs from happening by accident. A pty passes it, so it neither proves a
    person is there nor resists someone who means to get around it.
    """
    if not (_is_terminal(sys.stdin) and _is_terminal(out)):
        raise BoundaryError(
            f"review {command} needs a terminal on stdin and stdout; it does not run from a "
            "pipe or a non-interactive shell. Nothing was recorded."
        )


def _approve(args, env, out):
    # There is deliberately no flag or variable that skips this check.
    _require_terminal("approve", out)
    root = paths.state_root(env)
    run_dir = paths.open_run(root, args.run)

    def show(text):
        print(text, file=out)

    knowledge = knowledge_block.Options(
        scope=args.knowledge_scope,
        project=args.knowledge_project,
        component=args.knowledge_component,
        classification=args.knowledge_classification,
        readers=tuple(args.knowledge_reader or ()),
        enforcement=args.knowledge_enforcement,
        applies_when=tuple(args.knowledge_applies_when or ()),
        exceptions=tuple(args.knowledge_exception or ()),
    )
    summary = approve_stage.approve(
        run_dir,
        root,
        args.candidate,
        args.reviewer,
        args.reason,
        input,
        show,
        args.activate_as,
        knowledge,
    )
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _reject(args, env, out):
    root = paths.state_root(env)
    run_dir = paths.open_run(root, args.run)
    summary = approve_stage.reject(run_dir, root, args.candidate, args.reviewer, args.reason)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _retire(args, env, out):
    _require_terminal("retire", out)
    summary = approve_stage.retire(paths.state_root(env), args.entry, args.reviewer, args.reason)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK


def _timed(stage, handler):
    """Run a stage and add its wall-clock seconds to the run's timings.jsonl."""

    def run(args, env, out):
        started, started_at = timer(), records.iso()
        code = handler(args, env, out)
        run_id = getattr(args, "run", None)
        if run_id is not None:
            run_dir = paths.open_run(paths.state_root(env), run_id)
            path = paths.child(run_dir, evaluate_stage.TIMINGS)
            rows = records.read_jsonl(path) if path.is_file() else []
            rows.append(
                {
                    "stage": stage,
                    "started_at": started_at,
                    "seconds": round(timer() - started, 3),
                    "exit_code": code,
                }
            )
            records.write_jsonl(path, rows)
        return code

    return run


def _backend_options(parser):
    parser.add_argument("--run", required=True, help="run id printed by collect")
    parser.add_argument(
        "--backend",
        required=True,
        choices=llm.BACKENDS,
        help="claude sends PR text, comments and diffs to Anthropic; stub is fixed rules, no model",
    )
    parser.add_argument("--auth-mode", choices=llm.AUTH_MODES, help="required with claude")
    parser.add_argument(
        "--max-run-budget-usd",
        type=float,
        help="required with claude: stop calling once the run's recorded spend reaches this",
    )
    parser.add_argument(
        "--max-call-budget-usd",
        type=float,
        help="optional cap per claude call (default: what is left of the run budget, at most 100)",
    )
    parser.add_argument(
        "--deny-terms-file", help="terms that must not leave the machine; a hit stops with exit 3"
    )
    parser.add_argument("--model", help="model alias or id passed to the Claude CLI")
    parser.add_argument(
        "--timeout",
        type=int,
        default=llm.DEFAULT_TIMEOUT,
        help=f"seconds per claude call (default {llm.DEFAULT_TIMEOUT})",
    )


def register(groups):
    review = groups.add_parser(
        "review",
        help="review learning: collect, normalize, analyze, classify, split, generate, "
        "evaluate, approve",
    )
    stages = review.add_subparsers(dest="stage", required=True)

    collect = stages.add_parser("collect", help="read PRs, reviews and threads into a new run")
    collect.add_argument("--repo", required=True, help="OWNER/NAME")
    collect.add_argument("--since", required=True, help="UTC, e.g. 2026-01-01T00:00:00Z")
    collect.add_argument("--until", required=True, help="UTC, e.g. 2026-03-31T23:59:59Z")
    source = collect.add_mutually_exclusive_group(required=True)
    source.add_argument("--auth-env", help="name of the variable holding a read-only token")
    source.add_argument("--fixture", help="directory of synthetic responses; no network")
    collect.add_argument("--host", help="GitHub host, passed to gh as --hostname")
    collect.add_argument(
        "--max-wait",
        type=int,
        default=DEFAULT_MAX_WAIT,
        help=f"cap on total rate-limit waiting in seconds (default {DEFAULT_MAX_WAIT})",
    )
    collect.set_defaults(handler=_timed("collect", _collect))

    normalize = stages.add_parser("normalize", help="raw pages to threads.jsonl")
    normalize.add_argument("--run", required=True, help="run id printed by collect")
    normalize.set_defaults(handler=_timed("normalize", _normalize))

    analyze = stages.add_parser("analyze", help="threads to lessons.jsonl, checked against quotes")
    _backend_options(analyze)
    _split_option(analyze)
    analyze.set_defaults(handler=_timed("analyze", _analyze))

    classify = stages.add_parser("classify", help="lessons to targets, set-aside and conflicts")
    _backend_options(classify)
    classify.add_argument(
        "--existing-rules", required=True, help="directory of Markdown rule files (read only)"
    )
    classify.set_defaults(handler=_timed("classify", _classify))

    split = stages.add_parser("split", help="fix the time split between generation and evaluation")
    split.add_argument("--run", required=True, help="run id printed by collect")
    split.add_argument("--t1", required=True, help="UTC; generation uses events before it")
    split.add_argument(
        "--gap-days",
        type=int,
        default=0,
        help="evaluation PRs are created at or after t1 + this many days (default 0)",
    )
    split.add_argument(
        "--exclude-pr",
        type=int,
        action="append",
        default=[],
        metavar="N",
        help="leave this collected PR out of both sides (repeatable)",
    )
    split.set_defaults(handler=_timed("split", _split))

    generate = stages.add_parser("generate", help="classified lessons to candidates (proposed)")
    _backend_options(generate)
    _split_option(generate)
    generate.add_argument(
        "--include-untrusted",
        action="store_true",
        help="also build candidates from lessons raised only by outside accounts or bots",
    )
    generate.set_defaults(handler=_timed("generate", _generate))

    evaluate = stages.add_parser("evaluate", help="apply candidates to the evaluation PRs")
    _backend_options(evaluate)
    evaluate.add_argument("--split", required=True, help="split.json written by review split")
    evaluate.add_argument(
        "--adjudication", help="a person's verdicts on findings (schema review-adjudication)"
    )
    evaluate.add_argument(
        "--format",
        choices=evaluate_stage.FORMATS,
        default="summary",
        help="evaluator-output prints the improvement harness's evaluator JSON on stdout",
    )
    evaluate.add_argument("--seed", type=int, help="bootstrap seed (changes the criteria hash)")
    evaluate.add_argument(
        "--resamples", type=int, help="bootstrap resamples (changes the criteria hash)"
    )
    evaluate.set_defaults(handler=_timed("evaluate", _evaluate))

    approve = stages.add_parser(
        "approve", help="a person approves an evaluated candidate (needs a terminal)"
    )
    _decision_options(approve)
    approve.add_argument(
        "--activate-as",
        help="also write a Unified Diff adding the artifact at this path of your repository",
    )
    _knowledge_options(approve)
    approve.set_defaults(handler=_approve)

    reject = stages.add_parser("reject", help="record that a candidate is rejected")
    _decision_options(reject)
    reject.set_defaults(handler=_reject)

    retire = stages.add_parser("retire", help="retire an approved registry entry")
    retire.add_argument("--entry", required=True, help="registry id, RG-0001")
    retire.add_argument("--reviewer", required=True)
    retire.add_argument("--reason", required=True)
    retire.set_defaults(handler=_retire)


def _split_option(parser):
    parser.add_argument(
        "--split", help="split.json from review split: use only events before its t1"
    )


def _knowledge_options(parser):
    """Where the approved rule sits in the knowledge index (the `knowledge` block). Without any
    of them the registry version is written as before; with one, the rest comes from
    knowledge/config.json and the typed hash covers the block."""
    placed = parser.add_argument_group(
        "knowledge index", "the knowledge block (needs knowledge/config.json; shown and confirmed)"
    )
    placed.add_argument("--knowledge-scope", choices=knowledge_block.LEVELS)
    placed.add_argument("--knowledge-project", help="the project (project or component scope)")
    placed.add_argument("--knowledge-component", help="the component (component scope)")
    placed.add_argument(
        "--knowledge-classification",
        choices=knowledge_config.CLASSIFICATIONS,
        help="default: the project's default_access",
    )
    placed.add_argument(
        "--knowledge-reader", action="append", help="repeat for more; default: the project's"
    )
    placed.add_argument(
        "--knowledge-enforcement",
        choices=knowledge_block.ENFORCEMENTS,
        help="mandatory: every search in the scope returns it (default advisory)",
    )
    placed.add_argument(
        "--knowledge-applies-when", action="append", help="one short sentence; repeat for more"
    )
    placed.add_argument("--knowledge-exception", action="append", help="repeat for more")


def _decision_options(parser):
    parser.add_argument("--run", required=True, help="run id")
    parser.add_argument("--candidate", required=True, help="candidate id, IC-0001")
    parser.add_argument("--reviewer", required=True, help="who decides (recorded as given)")
    parser.add_argument("--reason", required=True, help="why")
