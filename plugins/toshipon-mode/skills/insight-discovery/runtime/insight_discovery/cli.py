"""Command line surface. Every command prints one JSON document (the Markdown report aside).

Exit codes: 0 done (including "nothing to present"), 1 a quality gate failed, 2 bad input or
settings, 3 a safety boundary refused.
"""

import argparse
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

from . import decisions, evidence, jobs, pipeline, ranking, report, routing
from . import state as st
from .adapters import ADAPTERS
from .foundation import (
    AUTOMATED_ENV,
    SKILL_ROOT,
    Busy,
    GateFailed,
    HarnessError,
    Queue,
    Refusal,
    dispatch,
    find_deny_terms,
    find_secrets,
    load_config,
    state_home,
    utcnow,
    write_json,
)

EXAMPLES = SKILL_ROOT / "examples" / "synthetic"
SCAN_LIMIT = 2_000_000


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessError(f"cannot read JSON from {path}: {error}") from None


def _parser():
    parser = argparse.ArgumentParser(prog="insight.py", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, text, *required):
        command = commands.add_parser(name, help=text)
        for flag in required:
            if flag.startswith("--"):
                command.add_argument(flag, required=True)
            else:
                command.add_argument(flag)
        return command

    ingest = add("ingest", "normalize one source into evidence", "--source")
    ingest.add_argument("--path")
    add("next", "what the ledger says to do next")
    step = commands.add_parser("step", help="run one stage by hand").add_subparsers(
        dest="step", required=True
    )
    step.add_parser("prompt").add_argument("stage", choices=st.STAGES)
    record = step.add_parser("record")
    record.add_argument("stage", choices=st.STAGES)
    record.add_argument("--file", required=True)
    record.add_argument("--independence", default="same_context")
    run = add("run", "ingest what is new and advance the loop within the limits")
    run.add_argument("--daily", action="store_true", help="the only mode; kept for readability")
    run.add_argument("--dry-run", action="store_true")
    add("weekly", "write the weekly report and wait for a person's choice")
    prior = add("prior", "record your own hypothesis before reading the generated ones")
    prior.add_argument("--text")
    prior.add_argument("--skip", action="store_true")
    prior.add_argument("--reason")
    add(
        "select", "a person accepts one candidate this week", "insight_id", "--reviewer", "--reason"
    )
    resolve = add("resolve", "reject a candidate, or mark an accepted one tested", "insight_id")
    resolve.add_argument("--status", required=True, choices=("rejected", "tested"))
    resolve.add_argument("--reason", required=True)
    resolve.add_argument("--actor", default="human")
    handoff = add("handoff", "hand a candidate to a neighbouring skill", "insight_id", "--reason")
    handoff.add_argument("--target", required=True, choices=sorted(routing.HANDOFFS.values()))
    search = commands.add_parser("search", help="prior-art search, by hand only").add_subparsers(
        dest="search", required=True
    )
    approve = search.add_parser("approve")
    approve.add_argument("insight_id")
    approve.add_argument("--query", action="append", required=True)
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--reason", required=True)
    found = search.add_parser("record")
    found.add_argument("insight_id")
    found.add_argument("--file", required=True)
    add("report", "the current report").add_argument(
        "--format", choices=("md", "json"), default="md"
    )
    add("status", "state of the loop, for a dashboard to read")
    add("scan", "look for secrets and forbidden terms in files").add_argument("paths", nargs="*")
    add("demo", "set up the synthetic example in a new directory", "directory")
    tick = add("schedule-tick", "scheduler entry point: queue due jobs, then work the queue")
    tick.add_argument("--dry-run", action="store_true")
    add("dispatch", "work the queue once, with the foundation's jobs and Insight's")
    return parser


def overview(home, config, settings, now):
    state = st.load(home)
    week = st.week_of(now)
    jobs = Queue(home, config["queue"]).jobs().values()
    return {
        "home": str(home),
        "evidence": len(state["evidence"]),
        "unanalyzed": len(st.fresh(state)),
        "next": pipeline.next_action(state),
        "candidates": dict(Counter(c["status"] for c in state["candidates"].values())),
        "presented": [candidate["id"] for candidate in ranking.presentable(state, settings)],
        "handoffs": len(state["handoffs"]),
        "week": week,
        "human_prior_recorded": week in state["priors"],
        "selected_this_week": state["selections"].get(week),
        "last_budget_stop": state["budget_stops"][-1] if state["budget_stops"] else None,
        "latest_report": state["reports"][-1]["report"] if state["reports"] else None,
        "queue": dict(Counter(job["status"] for job in jobs if job["kind"].startswith("insight_"))),
    }


def scan(paths, config):
    """Secret shapes and forbidden terms in the given files. Used before anything is made public."""
    terms, hits, checked = evidence.deny_terms(config), [], 0
    roots = [Path(path) for path in paths] or [EXAMPLES]
    files = [
        file
        for root in roots
        for file in (sorted(root.rglob("*")) if root.is_dir() else [root])
        if file.is_file() and not file.is_symlink()
    ]
    for file in files:
        if file.stat().st_size > SCAN_LIMIT:
            hits.append({"file": str(file), "found": ["too_large_to_scan"]})
            continue
        try:
            text = file.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        checked += 1
        found = find_secrets(text) + (["deny_term"] if find_deny_terms(text, terms) else [])
        if found:
            hits.append({"file": str(file), "found": found})
    if hits:
        raise Refusal(json.dumps({"scanned": checked, "hits": hits}, ensure_ascii=False))
    return {"scanned": checked, "hits": []}


def demo(directory):
    """Copy the synthetic example out of the repository and point a new state home at it."""
    target = Path(directory).expanduser().resolve()
    if target.exists():
        raise HarnessError(f"demo directory already exists: {target}")
    shutil.copytree(EXAMPLES, target)
    home = state_home({"AI_HARNESS_HOME": str(target / "state")})
    stub = {"argv": [sys.executable, str(target / "stub_agent.py")], "dry_run_safe": True}
    settings = {
        "schema_version": 1,
        "sources": [{"adapter": "manual", "path": str(target / "records")}],
        "agents": {"analyst": stub, "auditor": stub},
    }
    st.root(home).mkdir(mode=0o700, exist_ok=True)
    write_json(st.root(home) / "config.json", settings)
    return {"home": str(home), "export": f"export AI_HARNESS_HOME={home}", "directory": str(target)}


def _execute(args):
    if args.command == "demo":
        return demo(args.directory)
    home, now = state_home(), utcnow()
    config, settings = load_config(home), st.load_config(home)
    name = args.command
    if name == "ingest":
        if args.source not in ADAPTERS:
            raise HarnessError(f"--source must be one of {sorted(ADAPTERS)}")
        return {
            "adapter": args.source,
            "stats": evidence.ingest(home, args.source, args.path, config, settings, now),
        }
    if name == "next":
        return pipeline.next_action(st.load(home))
    if name == "step":
        if args.step == "prompt":
            return pipeline.step_prompt(home, config, settings, args.stage, now)
        output = _load(args.file)
        return pipeline.step_record(home, settings, args.stage, output, args.independence, now)
    if name == "run":
        if args.dry_run:
            return pipeline.plan(home, config, settings, now)
        return pipeline.run_daily(home, config, settings, now)
    if name == "weekly":
        return decisions.weekly(home, settings, now)
    if name == "prior":
        if args.skip:
            return decisions.skip_prior(home, args.reason, now)
        return decisions.prior(home, config, args.text, now)
    if name == "select":
        return decisions.select(home, settings, args.insight_id, args.reviewer, args.reason, now)
    if name == "resolve":
        return decisions.resolve(home, args.insight_id, args.status, args.reason, args.actor, now)
    if name == "handoff":
        return decisions.handoff(home, settings, args.insight_id, args.target, args.reason, now)
    if name == "search":
        if args.search == "approve":
            return decisions.search_approve(
                home, config, settings, args.insight_id, args.query, args.reviewer, args.reason, now
            )
        return decisions.search_record(
            home, config, settings, args.insight_id, _load(args.file), now
        )
    if name == "report":
        state = st.load(home)
        if args.format == "json":
            return report.as_json(state, settings, now)
        return report.render(state, settings, now)
    if name == "status":
        return overview(home, config, settings, now)
    if name in ("schedule-tick", "dispatch"):
        os.environ[AUTOMATED_ENV] = "1"  # human gates refuse from here on
        if name == "dispatch":
            return {"jobs": dispatch.dispatch(home, config, handlers=jobs.all_handlers())}
        return jobs.rehearse(home, config, now) if args.dry_run else jobs.tick(home, config, now)
    return scan(args.paths, config)


def main(argv=None):
    args = _parser().parse_args(argv)
    try:
        result = _execute(args)
    except Busy as busy:
        print(json.dumps({"skipped": str(busy)}, ensure_ascii=False))
        return 0
    except HarnessError as refusal:
        code = 3 if isinstance(refusal, Refusal) else 1 if isinstance(refusal, GateFailed) else 2
        print(json.dumps({"error": str(refusal)}, ensure_ascii=False), file=sys.stderr)
        return code
    if isinstance(result, str):
        print(result, end="")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
