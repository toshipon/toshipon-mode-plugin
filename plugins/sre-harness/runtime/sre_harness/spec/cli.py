"""`sre-harness spec <stage>`."""

import json
import sys
import time
from pathlib import Path

from .. import guard, llm, paths, privacy
from ..errors import EXIT_OK, EXIT_QUALITY, InputError
from ..github.fixture import FixtureTransport
from ..github.rest import GhTransport, Limiter
from . import extract as extract_stage
from . import ingest as ingest_stage

# Injected so tests can drive rate-limit waits without waiting.
clock = time.time
sleep = time.sleep

MAX_WAIT = 900
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


def _session(args, run_dir, env):
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
        print(SENDING.format(mode=args.auth_mode), file=sys.stderr)
    return session


def _extract(args, env, out):
    run_dir = paths.open_run(paths.state_root(env), args.run)
    summary = extract_stage.extract(run_dir, _session(args, run_dir, env))
    print(json.dumps(summary, indent=2, ensure_ascii=False), file=out)
    return EXIT_OK


def register(groups):
    spec = groups.add_parser("spec", help="specification consistency: ingest, extract")
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
    extract.add_argument(
        "--backend",
        required=True,
        choices=llm.BACKENDS,
        help="claude sends document sections to Anthropic; stub is fixed rules, no model",
    )
    extract.add_argument("--auth-mode", choices=llm.AUTH_MODES, help="required with claude")
    extract.add_argument(
        "--max-run-budget-usd",
        type=float,
        help="required with claude: stop calling once the run's recorded spend reaches this",
    )
    extract.add_argument("--max-call-budget-usd", type=float, help="optional cap per claude call")
    extract.add_argument(
        "--deny-terms-file", help="terms that must not leave the machine; a hit stops with exit 3"
    )
    extract.add_argument("--model", help="model alias or id passed to the Claude CLI")
    extract.add_argument(
        "--timeout",
        type=int,
        default=llm.DEFAULT_TIMEOUT,
        help=f"seconds per claude call (default {llm.DEFAULT_TIMEOUT})",
    )
    extract.set_defaults(handler=_extract)
