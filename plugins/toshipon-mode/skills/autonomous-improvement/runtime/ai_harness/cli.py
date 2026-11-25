"""Command line surface. Every command prints one JSON document; refusals exit 2."""

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import dispatch, evolve, guard, loop, record, status
from .jobs import Queue
from .store import (
    AUTOMATED_ENV,
    Busy,
    HarnessError,
    ensure_home,
    load_config,
    state_home,
    utcnow,
    write_json,
)

ROOT = Path(__file__).resolve().parents[2]


def _load(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise HarnessError(f"cannot read JSON from {path}: {error}") from None


def _parser():
    parser = argparse.ArgumentParser(prog="harness.py", description=__doc__)
    groups = parser.add_subparsers(dest="group", required=True)

    def group(name, text):
        return groups.add_parser(name, help=text).add_subparsers(dest="command", required=True)

    def add(commands, name, *arguments, **options):
        command = commands.add_parser(name, **options)
        for argument in arguments:
            flags = argument if isinstance(argument, tuple) else (argument,)
            extra = {"required": True} if flags[0].startswith("--") else {}
            command.add_argument(*flags, **extra)
        return command

    run = group("run", "product improvement run")
    add(run, "init", "--project", "--spec").add_argument("--run-id")
    for name in ("baseline", "next", "status", "report"):
        add(run, name, "run_id")
    add(run, "stop", "run_id", "--reason").add_argument("--note", default="")
    add(run, "unblock", "run_id", "--reviewer", "--reason")
    add(run, "criteria-change", "run_id", "--spec", "--reviewer", "--reason")

    hypothesis = group("hypothesis", "hypothesis ledger")
    add(hypothesis, "add", "run_id", "--file")

    experiment = group("experiment", "one experiment at a time")
    add(experiment, "start", "run_id", "hypothesis_id")
    finish = add(experiment, "finish", "run_id", "hypothesis_id")
    finish.add_argument("--cost", type=float, default=0.0)
    finish.add_argument("--downgrade-reason", help="reject an adoptable result; never the reverse")

    audit = group("audit", "independent audit of a run")
    add(audit, "record", "run_id", "--file", "--independence")
    add(audit, "resolve", "run_id", "audit_id", "finding_id", "--status", "--evidence")

    evo = group("evolve", "harness evolution (meta improvement)")
    add(evo, "analyze")
    add(evo, "suite-approve", "--file", "--reviewer", "--reason")
    add(evo, "hypothesis-add", "--file")
    for name in ("candidate-create", "candidate-freeze", "compare", "propose"):
        add(evo, name, "hypothesis_id")
    add(evo, "audit-record", "hypothesis_id", "--file", "--independence")
    add(evo, "audit-resolve", "hypothesis_id", "finding_id", "--status", "--evidence")
    add(evo, "close", "hypothesis_id", "--outcome", "--reason")
    add(evo, "approve", "proposal_id", "--reviewer", "--reason")
    add(evo, "publish-check", "proposal_id")
    add(evo, "cleanup")

    queue = group("queue", "persistent job queue")
    add(queue, "list")
    add(queue, "cancel", "job_id")
    add(queue, "retry", "job_id")

    groups.add_parser("record", help="hook entry point: reads the hook JSON on stdin")
    work = groups.add_parser("dispatch", help="work the queue once")
    work.add_argument("--dry-run", action="store_true")
    work.add_argument("--max-jobs", type=int)
    tick = groups.add_parser(
        "schedule-tick", help="scheduler entry point: queue due jobs, then dispatch"
    )
    tick.add_argument("--dry-run", action="store_true")
    groups.add_parser("status", help="overview of runs, queue and evolution")
    clean = groups.add_parser("gc", help="apply retention, or delete all recorded state")
    clean.add_argument("--all", action="store_true")
    demo = groups.add_parser("demo", help="set up the synthetic example in a new directory")
    demo.add_argument("directory")
    return parser


def _run(args, home, now):
    name = args.command
    if name == "init":
        run_id = loop.init(home, args.project, _load(args.spec), now, args.run_id)
        return {"run_id": run_id, "next": {"action": "baseline"}}
    if name == "baseline":
        return loop.baseline(home, args.run_id, now)
    if name == "next":
        return loop.next_action(loop.load(home, args.run_id))
    if name == "status":
        return loop.status(home, args.run_id)
    if name == "report":
        return {"report": loop.report(home, args.run_id)}
    if name == "stop":
        return loop.stop(home, args.run_id, args.reason, args.note, now)
    if name == "unblock":
        loop.unblock(home, args.run_id, args.reviewer, args.reason, now)
    else:
        loop.criteria_change(home, args.run_id, _load(args.spec), args.reviewer, args.reason, now)
    return loop.next_action(loop.load(home, args.run_id))


def _evolve(args, home, config, now):
    name, hypothesis_id = args.command, getattr(args, "hypothesis_id", None)
    if name == "analyze":
        patterns = evolve.analyze(home, config)
        return {
            "eligible": [p["signature"] for p in evolve.eligible(patterns)],
            "patterns": patterns,
        }
    if name == "suite-approve":
        return {
            "suite_hash": evolve.suite_approve(home, args.file, args.reviewer, args.reason, now)
        }
    if name == "hypothesis-add":
        return {"hypothesis_id": evolve.hypothesis_add(home, _load(args.file), now)}
    if name == "candidate-create":
        return {"worktree": str(evolve.candidate_create(home, hypothesis_id))}
    if name == "candidate-freeze":
        return evolve.candidate_freeze(home, hypothesis_id, now)
    if name == "compare":
        return evolve.compare(home, hypothesis_id, now)
    if name == "audit-record":
        evolve.audit_record(home, hypothesis_id, _load(args.file), args.independence, now)
    elif name == "audit-resolve":
        evolve.audit_resolve(home, hypothesis_id, args.finding_id, args.status, args.evidence, now)
    elif name == "close":
        evolve.close(home, hypothesis_id, args.outcome, args.reason, now)
    elif name == "propose":
        return evolve.propose(home, hypothesis_id, now)
    elif name == "approve":
        evolve.approve(home, args.proposal_id, args.reviewer, args.reason, now)
    elif name == "publish-check":
        return evolve.publish_check(home, config, args.proposal_id)
    else:
        return {"removed": evolve.cleanup(home)}
    return status.overview(home, config)["evolution"]


def _demo(directory):
    """Copy the synthetic example out of the repository and make its two git repositories."""
    target = Path(directory).expanduser().resolve()
    if target.exists():
        raise HarnessError(f"demo directory already exists: {target}")
    shutil.copytree(ROOT / "examples" / "synthetic", target)
    home = ensure_home(target / "state")
    for repo in (target / "project", target / "evolution" / "subject"):
        for command in (
            ["init", "-q", "-b", "main"],
            ["add", "-A"],
            [
                "-c",
                "user.name=demo",
                "-c",
                "user.email=demo@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-q",
                "-m",
                "synthetic baseline",
            ],
        ):
            guard.git(repo, *command)
    config = _load(target / "evolution" / "config.json")
    for adapter in config["agents"].values():
        adapter["argv"] = [sys.executable, str(target / "evolution" / adapter["argv"][1])]
    write_json(home / "config.json", config)
    return {"home": str(home), "export": f"export AI_HARNESS_HOME={home}", "directory": str(target)}


def _execute(args):
    if args.group == "demo":
        return _demo(args.directory)
    home, now = state_home(), utcnow()
    config = load_config(home)
    queue = Queue(home, config["queue"])
    if args.group == "run":
        return _run(args, home, now)
    if args.group == "hypothesis":
        return {"hypothesis_id": loop.hypothesis_add(home, args.run_id, _load(args.file), now)}
    if args.group == "experiment":
        if args.command == "start":
            return loop.experiment_start(home, args.run_id, args.hypothesis_id, now)
        decision = loop.experiment_finish(
            home, args.run_id, args.hypothesis_id, now, args.cost, args.downgrade_reason, queue
        )
        return {**decision, "next": loop.next_action(loop.load(home, args.run_id))}
    if args.group == "audit":
        if args.command == "record":
            audit_id = loop.audit_record(
                home, args.run_id, _load(args.file), args.independence, now
            )
            return {"audit_id": audit_id, "next": loop.next_action(loop.load(home, args.run_id))}
        loop.audit_resolve(
            home, args.run_id, args.audit_id, args.finding_id, args.status, args.evidence, now
        )
        return loop.next_action(loop.load(home, args.run_id))
    if args.group == "evolve":
        return _evolve(args, home, config, now)
    if args.group == "queue":
        if args.command == "list":
            return {"jobs": list(queue.jobs().values())}
        action = queue.cancel if args.command == "cancel" else queue.retry
        return {"changed": action(args.job_id, now)}
    if args.group in ("dispatch", "schedule-tick"):
        os.environ[AUTOMATED_ENV] = "1"
        tick = args.group == "schedule-tick"
        if args.dry_run:
            return dispatch.dry_run(home, now, tick=tick)
        queued = dispatch.schedule_tick(home, config, queue, now) if tick else []
        return {
            "queued": queued,
            "jobs": dispatch.dispatch(home, config, max_jobs=getattr(args, "max_jobs", None)),
        }
    if args.group == "status":
        return status.overview(home, config)
    return status.gc(home, config, now, everything=args.all)


def main(argv=None):
    args = _parser().parse_args(argv)
    if args.group == "record":
        # A hook must never fail or slow the session it observes: swallow everything, print nothing.
        try:
            home = state_home()
            config = load_config(home)
            payload = json.loads(sys.stdin.read() or "{}")
            record.record(payload, home, config, Queue(home, config["queue"]), utcnow(), os.environ)
        except Exception:
            pass
        return 0
    try:
        result = _execute(args)
    except Busy as busy:
        print(json.dumps({"skipped": str(busy)}, ensure_ascii=False))
        return 0
    except HarnessError as refusal:
        print(json.dumps({"error": str(refusal)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
