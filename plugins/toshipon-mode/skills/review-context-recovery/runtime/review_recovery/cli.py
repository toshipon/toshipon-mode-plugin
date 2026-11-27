"""Command line of Review Context Recovery. It reads the repository and the files the session
hands it. It does not call GitHub, and it has no command that changes a PR or the code."""

import argparse
import json
import sys
from pathlib import Path

from .foundation import Refusal, dump, parse_locator, read_json, read_text
from .session import ASSESSMENTS, Session


def _run(args):
    session = Session(Path(args.root).resolve(), args.pr)
    if args.command == "start":
        context = read_text(args.context_file, "session context") if args.context_file else ""
        return session.start(
            args.base, args.head, read_json(args.threads, "threads"), args.comment, context
        )
    if args.command == "next":
        return session.next()
    if args.command == "explain":
        return session.explain(read_json(args.file, "explanation"))
    if args.command == "answer":
        return session.answer(args.text, args.assessment, args.correction)
    if args.command == "recap":
        return session.recap(args.intent, args.risk, args.next_action)
    if args.command == "evidence":
        session.state()  # a snapshot belongs to a started session
        if parse_locator(args.locator).get("comment"):
            raise Refusal(
                "review comments are saved by start; they are not replaced or added later"
            )
        return session.evidence.add(
            args.locator, args.revision, read_text(args.file, "source text")
        )
    if args.command == "skip":
        return session.skip(args.reason)
    return session.status()


def parser():
    top = argparse.ArgumentParser(prog="review-context")
    top.add_argument("--root", default=".", help="repository root")
    top.add_argument("--pr", type=int, required=True, help="pull request number")
    commands = top.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    start.add_argument("--base", required=True, help="base commit sha")
    start.add_argument("--head", required=True, help="head commit sha")
    start.add_argument("--threads", required=True, help="review comments as JSON")
    start.add_argument("--comment", type=int, help="recover only the thread holding this comment")
    start.add_argument(
        "--context-file", help="what the user was doing and already knows (optional)"
    )
    commands.add_parser("next")
    commands.add_parser("explain").add_argument("--file", required=True)
    answer = commands.add_parser("answer")
    answer.add_argument("--text", required=True, help="the user's answer, in their words")
    answer.add_argument("--assessment", required=True, choices=ASSESSMENTS)
    answer.add_argument("--correction", default="")
    recap = commands.add_parser("recap")
    for name in ("intent", "risk", "next-action"):
        recap.add_argument(f"--{name}", required=True)
    evidence = commands.add_parser("evidence", help="save a fetched PR description or document")
    evidence.add_argument("--locator", required=True)
    evidence.add_argument("--revision", required=True)
    evidence.add_argument("--file", required=True)
    commands.add_parser("skip").add_argument("--reason", required=True)
    commands.add_parser("status")
    return top


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        result = _run(args)
    except Refusal as refused:
        print(
            json.dumps({"error": str(refused), "problems": refused.problems}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    print(dump(result), end="")
    return 0
