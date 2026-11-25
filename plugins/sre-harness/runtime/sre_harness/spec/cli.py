"""`sre-harness spec <stage>`."""

import json
import sys
import time
from pathlib import Path

from .. import guard, llm, paths, privacy, records
from ..errors import EXIT_OK, EXIT_QUALITY, InputError
from ..github.fixture import FixtureTransport
from ..github.rest import GhTransport, Limiter
from . import analyze as analyze_stage
from . import extract as extract_stage
from . import ingest as ingest_stage
from . import patch as patch_stage
from . import report as report_stage

# Injected so tests can drive rate-limit waits without waiting.
clock = time.time
sleep = time.sleep

MAX_WAIT = 900
ANALYZE_SENDING = (
    "note: --backend claude sends default-branch Markdown that names no id, the registry's "
    "titles, and the semantic-conflict candidates (PR numbers, titles, values) to Anthropic "
    "through the Claude CLI (no tools, no MCP servers, auth mode {mode})."
)
SENDING = (
    "note: --backend claude sends the run's document sections (Markdown, ADRs, minutes, PR "
    "bodies and comments, Slack and Confluence exports) to Anthropic through the Claude CLI "
    "(no tools, no MCP servers, auth mode {mode})."
)


def _ingest(args, env, out):
    transport, source = None, {"kind": "files"}
    if args.fixture is not None:
        transport, source = FixtureTransport(Path(args.fixture)), {"kind": "fixture"}
    elif args.auth_env is not None:
        # The token is read before anything else; an empty variable stops here, unsent.
        token = guard.read_token(args.auth_env, env)
        transport = GhTransport(token, host=args.host, env=env)
        source = {"kind": "github", "auth_env": args.auth_env}
    if args.host is not None:
        source["host"] = args.host
    limiter = Limiter(clock, sleep, MAX_WAIT)
    summary = ingest_stage.ingest(Path(args.manifest), transport, limiter, source, env)
    print(json.dumps(summary, indent=2), file=out)
    return EXIT_OK if summary["status"] == "complete" else EXIT_QUALITY


def _session(args, run_dir, env, notice=None):
    low, high = guard.CLAUDE_TIMEOUT
    if not low <= args.timeout <= high:
        raise InputError(f"--timeout must be {low}-{high} seconds.")
    session = llm.Session(
        run_dir,
        args.backend,
        auth_mode=args.auth_mode,
        max_run_budget_usd=args.max_run_budget_usd,
        max_call_budget_usd=args.max_call_budget_usd,
        deny_terms=privacy.load_deny_terms(args.deny_terms_file),
        timeout=args.timeout,
        model=args.model,
        env=env,
    )
    if args.backend == "claude":
        print((notice or SENDING).format(mode=args.auth_mode), file=sys.stderr)
    return session


def _extract(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    summary = extract_stage.extract(run_dir, _session(args, run_dir, env))
    print(json.dumps(summary, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def _repo(value):
    owner, _, name = value.partition("/")
    # The guard's own patterns check the names before they reach a path or a request.
    guard.rest_path("pulls", {"owner": owner, "repo": name})
    return owner, name


def _analyze(args, env, out):
    root = paths.state_root(env)
    run_dir = paths.open_run(root, args.run)
    owner, name = _repo(args.repo)
    transport, source, token = _sources(args, env, owner, name)
    session = None
    if args.backend is not None:
        session = _session(args, run_dir, env, ANALYZE_SENDING)
    context = {
        "transport": transport,
        "limiter": Limiter(clock, sleep, MAX_WAIT),
        "repo": (owner, name),
        "source": source,
        "token": token,
        "root": root,
        "default_branch": args.default_branch,
        "session": session,
    }
    summary, code = analyze_stage.analyze(run_dir, context)
    print(json.dumps(summary, indent=2, ensure_ascii=False), file=out)
    return code


def _report(args, env, out):
    root = paths.state_root(env)
    run_dir = paths.open_run(root, args.run)
    value, _ = report_stage.report(run_dir, root)
    if args.format == "evaluator-output":
        print(json.dumps(report_stage.evaluator_output(value), indent=2), file=out)
    else:
        counts = {}
        for item in value["findings"]:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        summary = {
            "run_id": value["run_id"],
            "status": value["status"],
            "findings": counts,
            "patches": [item["id"] for item in value["patches"]],
            "patch_rejections": len(value["patch_rejections"]),
            "files": ["impact-report.json", "impact-report.md", "patches/"],
        }
        print(json.dumps(summary, indent=2), file=out)
    return EXIT_QUALITY if value["status"] != "complete" else EXIT_OK


def _sources(args, env, owner, name):
    """(transport, git source, token) from --auth-env or --fixture with --git-source. The token
    is read before anything else; an empty variable stops here, unsent."""
    if args.auth_env is not None:
        token = guard.read_token(args.auth_env, env)
        if args.git_source is not None:
            raise InputError(
                "--git-source is for --fixture runs; with --auth-env git fetches "
                "from the GitHub host."
            )
        transport = GhTransport(token, host=args.host, env=env)
        return transport, f"https://{args.host or 'github.com'}/{owner}/{name}.git", token
    if args.git_source is None:
        raise InputError("--fixture needs --git-source <absolute path of a local repository>.")
    local = Path(args.git_source)
    if not local.is_absolute() or not local.is_dir():
        raise InputError(f"--git-source must be an absolute directory: {args.git_source}")
    return FixtureTransport(Path(args.fixture)), str(local), None


def _check_patch(args, env, out):
    root = paths.state_root(env)
    if args.auth_env is not None:
        guard.read_token(args.auth_env, env)  # an empty variable stops before anything else
    name = patch_stage.patch_id(args.patch)
    run_dir = paths.open_run(root, args.run)
    findings = paths.child(run_dir, "findings.json")
    if not findings.is_file():
        raise InputError("the run has no findings yet: run `spec analyze` first.")
    owner, repo = _repo(records.read_json(findings)["repo"])
    transport, source, token = _sources(args, env, owner, repo)
    context = {
        "transport": transport,
        "limiter": Limiter(clock, sleep, MAX_WAIT),
        "source": source,
        "token": token,
        "root": root,
    }
    output, code = patch_stage.check(run_dir, name, context)
    print(json.dumps(output, indent=2, ensure_ascii=False), file=out)
    return code


def _llm_arguments(parser, required):
    parser.add_argument(
        "--backend",
        required=required,
        choices=llm.BACKENDS,
        help="claude sends document text to Anthropic; stub is fixed rules, no model",
    )
    parser.add_argument("--auth-mode", choices=llm.AUTH_MODES, help="required with claude")
    parser.add_argument(
        "--max-run-budget-usd",
        type=float,
        help="required with claude: stop calling once the run's recorded spend reaches this",
    )
    parser.add_argument("--max-call-budget-usd", type=float, help="optional cap per claude call")
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
    spec = groups.add_parser(
        "spec", help="specification consistency: ingest, extract, analyze, report"
    )
    stages = spec.add_subparsers(dest="stage", required=True)

    ingest = stages.add_parser("ingest", help="files and GitHub PRs to documents.jsonl")
    ingest.add_argument("--manifest", required=True, help="JSON manifest; paths are relative to it")
    source = ingest.add_mutually_exclusive_group()
    source.add_argument("--auth-env", help="variable holding a read-only token (GitHub entries)")
    source.add_argument("--fixture", help="directory of synthetic GitHub responses; no network")
    ingest.add_argument("--host", help="GitHub host, passed to gh as --hostname")
    ingest.set_defaults(handler=_ingest)

    extract = stages.add_parser("extract", help="documents to decisions.json, requirements.json")
    extract.add_argument("--run", required=True, help="run id printed by ingest")
    _llm_arguments(extract, required=True)
    extract.set_defaults(handler=_extract)

    analyze = stages.add_parser("analyze", help="registry, default branch, open PRs to findings")
    analyze.add_argument("--run", required=True, help="run id that `spec extract` filled")
    analyze.add_argument("--prs", required=True, choices=["open"], help="which PRs: open")
    analyze.add_argument("--repo", required=True, help="owner/name of the GitHub repository")
    source = analyze.add_mutually_exclusive_group(required=True)
    source.add_argument("--auth-env", help="variable holding a read-only token")
    source.add_argument("--fixture", help="directory of synthetic GitHub responses; no network")
    analyze.add_argument(
        "--git-source", help="with --fixture: absolute path of a local repository to fetch from"
    )
    analyze.add_argument("--host", help="GitHub host, passed to gh as --hostname")
    analyze.add_argument("--default-branch", help="default branch name (else from the PR list)")
    _llm_arguments(analyze, required=False)
    analyze.set_defaults(handler=_analyze)

    report = stages.add_parser("report", help="findings to impact-report.json and .md")
    report.add_argument("--run", required=True, help="run id that `spec analyze` filled")
    report.add_argument(
        "--format",
        choices=["summary", "evaluator-output"],
        default="summary",
        help="evaluator-output prints the v2 evaluator adapter JSON on stdout",
    )
    report.set_defaults(handler=_report)

    check = stages.add_parser(
        "check-patch",
        help="right before applying: re-read the head a patch is for and check it again",
    )
    check.add_argument("--run", required=True, help="run id that `spec report` filled")
    check.add_argument("--patch", required=True, help="patch id from impact-report.json (PT-0001)")
    source = check.add_mutually_exclusive_group(required=True)
    source.add_argument("--auth-env", help="variable holding a read-only token")
    source.add_argument("--fixture", help="directory of synthetic GitHub responses; no network")
    check.add_argument(
        "--git-source", help="with --fixture: absolute path of a local repository to fetch from"
    )
    check.add_argument("--host", help="GitHub host, passed to gh as --hostname")
    check.set_defaults(handler=_check_patch)
