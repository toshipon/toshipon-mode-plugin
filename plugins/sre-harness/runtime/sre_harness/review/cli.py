"""`sre-harness review <stage>`."""

import json
import re
import time
from pathlib import Path

from .. import guard, paths
from ..errors import EXIT_OK, EXIT_QUALITY, InputError
from ..github.fixture import FixtureTransport
from ..github.rest import GhTransport, Limiter
from . import collect as collect_stage
from . import normalize as normalize_stage

# Injected so tests can drive rate-limit waits without waiting.
clock = time.time
sleep = time.sleep

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


def register(groups):
    review = groups.add_parser("review", help="review learning: collect and normalize")
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
    collect.set_defaults(handler=_collect)

    normalize = stages.add_parser("normalize", help="raw pages to threads.jsonl")
    normalize.add_argument("--run", required=True, help="run id printed by collect")
    normalize.set_defaults(handler=_normalize)
