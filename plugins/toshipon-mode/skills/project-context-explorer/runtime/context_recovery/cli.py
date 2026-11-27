"""Command line of the Project Context Explorer. Every command prints JSON. Nothing here
writes to the project, calls a network or runs a tool the session did not already run."""

import argparse
import json
import re
import sys
from pathlib import Path

from . import index as ix
from . import learning
from .base import Refusal, dump, project_dir, read_json, read_text, write_private
from .claims import verify
from .evidence import Evidence
from .explorer import render
from .packet import MODES, build, overview
from .traverse import explore

_SCOPE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def outside_repository(path):
    """An output path that no repository would pick up and publish."""
    path = Path(path).expanduser().resolve()
    for parent in path.parents:
        if (parent / ".git").exists():
            raise Refusal(f"output must be outside any git repository: {path}")
    return path


class Workspace:
    def __init__(self, root, scope):
        if not _SCOPE.match(scope):
            raise Refusal(f"scope must match {_SCOPE.pattern}: {scope!r}")
        self.root = Path(root).resolve()
        if not self.root.is_dir():
            raise Refusal(f"project root is not a directory: {root}")
        self.home = project_dir("context-explorer", self.root, scope)
        self.index_path = self.home / "index.json"
        self.learning_path = self.home / "learning.jsonl"
        self.evidence = Evidence(self.root, self.home / "evidence")

    def index(self):
        return ix.load(self.index_path, self.root)

    def save(self, index):
        write_private(self.index_path, dump(index))


def _exploration(space, args):
    if not args.entity and not args.question:
        raise Refusal("give --entity, --question or both")
    return explore(
        space.index(),
        entity=args.entity,
        question=args.question or "",
        hops=args.hops,
        max_entities=args.max_entities,
        token_budget=args.token_budget,
    )


def _run(args):
    space = Workspace(args.root, args.scope)
    command = args.command
    if command == "path":
        return {"home": str(space.home), "index": str(space.index_path)}
    if command == "sources":
        index = ix.set_sources(space.index(), read_json(args.file, "sources"))
        space.save(index)
        return {"sources": index["sources"]}
    if command == "index":
        index = space.index()
        if args.action == "add":
            entities = read_json(args.file, "entities")
        elif args.action == "import-registry":
            given = {key: getattr(args, key) for key in ("decisions", "requirements", "trace")}
            if not any(given.values()):
                raise Refusal("give at least one of --decisions, --requirements, --trace")
            entities = ix.from_registry(**{k: read_json(v, k) for k, v in given.items() if v})
        else:
            rows = [e for e in index["entities"].values() if args.type in (None, e["type"])]
            rows.sort(key=lambda entity: entity["id"])
            return {"entities": rows[: args.limit], "more": max(0, len(rows) - args.limit)}
        before = set(index["entities"])
        space.save(ix.upsert(index, entities, space.root))
        names = [entity["id"] for entity in entities]
        return {
            "added": sorted(set(names) - before),
            "replaced": sorted(set(names) & before),
            "entities": len(index["entities"]),
        }
    if command == "check":
        return ix.check(space.index(), space.root)
    if command == "overview":
        index = space.index()
        return overview(index, ix.check(index, space.root)["findings"], args.per_section)
    if command == "explore":
        return _exploration(space, args)
    if command == "evidence":
        return space.evidence.add(args.locator, args.revision, read_text(args.file, "source text"))
    if command == "claims":
        return verify(read_json(args.file, "claims"), space.evidence.resolve)
    if command == "packet":
        claims = read_json(args.claims, "claims") if args.claims else []
        if claims:
            report = verify(claims, space.evidence.resolve)
            if report["unsupported"]:
                problems = [p for result in report["results"] for p in result["problems"]]
                raise Refusal("a packet carries only claims whose evidence reads back", problems)
        findings = ix.check(space.index(), space.root)["findings"]
        packet = build(
            _exploration(space, args),
            question=args.question or "",
            mode=args.mode,
            findings=findings,
            claims=claims,
        )
        if args.out:
            out = outside_repository(args.out)
            write_private(out, packet["prompt"])
            return {"written": str(out), "token_budget": packet["token_budget"]}
        return packet
    if command == "learn":
        if args.action == "add":
            return learning.add(
                space.learning_path,
                read_json(args.file, "questions"),
                space.index(),
                space.evidence.resolve,
            )
        if args.action == "next":
            return {"question": learning.next_question(space.learning_path)}
        if args.action == "answer":
            record = learning.record_answer(
                space.learning_path, args.question_id, args.result, args.note
            )
            return {
                "recorded": record,
                "reveal": learning.reveal(space.learning_path, args.question_id),
            }
        return learning.weak_areas(space.learning_path)
    index = space.index()
    out = outside_repository(args.out) if args.out else space.home / "explorer.html"
    write_private(out, render(index, ix.check(index, space.root), args.title))
    return {"written": str(out), "entities": len(index["entities"])}


def _bounds(parser):
    parser.add_argument("--entity", help="start from this entity")
    parser.add_argument(
        "--question", help="the question; also picks the start when --entity is absent"
    )
    parser.add_argument("--hops", type=int, default=1)
    parser.add_argument("--max-entities", type=int, default=12)
    parser.add_argument("--token-budget", type=int, default=4000)


def parser():
    top = argparse.ArgumentParser(prog="context-explorer")
    top.add_argument("--root", default=".", help="project root (a git work tree)")
    top.add_argument("--scope", default="default", help="name of the index inside the project")
    commands = top.add_subparsers(dest="command", required=True)
    commands.add_parser("path")
    commands.add_parser("sources").add_argument("--file", required=True)
    index = commands.add_parser("index").add_subparsers(dest="action", required=True)
    index.add_parser("add").add_argument("--file", required=True)
    registry = index.add_parser("import-registry")
    for name in ("decisions", "requirements", "trace"):
        registry.add_argument(f"--{name}")
    show = index.add_parser("show")
    show.add_argument("--type", choices=ix.TYPES)
    show.add_argument("--limit", type=int, default=20)
    commands.add_parser("check")
    commands.add_parser("overview").add_argument("--per-section", type=int, default=5)
    _bounds(commands.add_parser("explore"))
    evidence = (
        commands.add_parser("evidence")
        .add_subparsers(dest="action", required=True)
        .add_parser("add")
    )
    evidence.add_argument("--locator", required=True)
    evidence.add_argument("--revision", required=True)
    evidence.add_argument("--file", required=True)
    claims = (
        commands.add_parser("claims")
        .add_subparsers(dest="action", required=True)
        .add_parser("check")
    )
    claims.add_argument("--file", required=True)
    packet = commands.add_parser("packet")
    _bounds(packet)
    packet.add_argument("--mode", choices=MODES, default="deep-dive")
    packet.add_argument("--claims")
    packet.add_argument("--out")
    learn = commands.add_parser("learn").add_subparsers(dest="action", required=True)
    learn.add_parser("add").add_argument("--file", required=True)
    learn.add_parser("next")
    answer = learn.add_parser("answer")
    answer.add_argument("question_id")
    answer.add_argument("--result", required=True, choices=learning.RESULTS)
    answer.add_argument("--note", default="")
    learn.add_parser("weak")
    page = commands.add_parser("html")
    page.add_argument("--out")
    page.add_argument("--title", default="Project Context Explorer")
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
    return 1 if args.command == "claims" and result["unsupported"] else 0
