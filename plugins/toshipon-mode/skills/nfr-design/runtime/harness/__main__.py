"""Command-line entry point for workspace generation and evaluation."""

import argparse
import json
import subprocess
import sys
from functools import partial
from pathlib import Path

from jsonschema.exceptions import ValidationError

from .claims import extract_claims, prepare_input, review_claim
from .context_cli import assess, write_impact
from .intake import collect
from .runner import claude_backend, demo_backend, evaluate_candidate, run_loop


def load(path):
    def reject(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    def unique(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON properties are not allowed.")
            result[key] = value
        return result

    return json.loads(
        Path(path).read_text(encoding="utf-8"), parse_constant=reject, object_pairs_hook=unique
    )


def main():
    parser = argparse.ArgumentParser(description="NFR / AWS document quality harness")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ["run", "demo", "evaluate"]:
        child = sub.add_parser(command)
        child.add_argument("--input", required=True)
        child.add_argument("--workspace", required=True)
        child.add_argument("--out", required=True)
        if command == "evaluate":
            child.add_argument("--design", required=True)
            child.add_argument("--review", required=True)
        else:
            child.add_argument("--max-iterations", type=int, default=3)
            if command == "run":
                child.add_argument(
                    "--auth-mode", choices=["api-key", "subscription"], default="api-key"
                )
    child = sub.add_parser("collect")
    child.add_argument("--manifest", required=True)
    child.add_argument("--out", required=True)
    child = sub.add_parser("extract")
    child.add_argument("--workspace", required=True)
    child.add_argument("--auth-mode", choices=["api-key", "subscription"], default="api-key")
    child.add_argument(
        "--proposals",
        help="Validate candidate claims from a local extraction instead of calling Claude.",
    )
    child = sub.add_parser("review")
    child.add_argument("--workspace", required=True)
    child.add_argument("--claim", required=True)
    child.add_argument("--status", choices=["approved", "rejected"], required=True)
    child.add_argument("--reviewer", required=True)
    child.add_argument("--reason", required=True)
    child.add_argument("--requirement-id")
    child.add_argument("--supersedes", nargs="*", default=None)
    child = sub.add_parser("assess")
    child.add_argument("--workspace", required=True)
    child.add_argument("--out", required=True)
    child = sub.add_parser("prepare")
    child.add_argument("--workspace", required=True)
    child.add_argument("--evidence", required=True)
    child.add_argument("--out", required=True)
    child = sub.add_parser("diff")
    child.add_argument("--before", required=True)
    child.add_argument("--after", required=True)
    child.add_argument("--input", required=True)
    child.add_argument("--design")
    child.add_argument("--out", required=True)
    args = parser.parse_args()
    try:
        if args.command == "collect":
            result = collect(args.manifest, args.out)
            print(f"Collected {len(result['sources'])} source snapshots. Claims remain unreviewed.")
            return 0
        if args.command == "extract":
            backend = partial(claude_backend, auth_mode=args.auth_mode)
            if args.proposals:
                proposals = load(args.proposals)

                def backend(role, payload, output_schema):
                    return proposals

            result = extract_claims(args.workspace, backend)
            print(f"Extracted {len(result['claims'])} claims. Explicit review is required.")
            return 0
        if args.command == "review":
            review_claim(
                args.workspace,
                args.claim,
                args.status,
                args.reviewer,
                args.reason,
                requirement_id=args.requirement_id,
                supersedes=args.supersedes,
            )
            print("Review recorded. Run assess before preparing design input.")
            return 0
        if args.command == "assess":
            result = assess(args.workspace, args.out)
            print(
                "PASS (context consistency only)"
                if result["passed"]
                else "FAIL (see context-report.json)"
            )
            return 0 if result["passed"] else 1
        if args.command == "prepare":
            result = prepare_input(args.workspace, args.evidence, args.out)
            print(f"Prepared {len(result['requirements'])} explicitly approved requirements.")
            return 0
        if args.command == "diff":
            result = write_impact(
                args.before,
                args.after,
                load(args.input),
                load(args.design) if args.design else None,
                args.out,
            )
            print(
                f"Changed sources: {len(result['changed_sources'])}; affected requirements: {len(result['affected_requirements'])}."
            )
            return 0
        data = load(args.input)
        if args.command == "evaluate":
            passed = evaluate_candidate(
                data,
                load(args.design),
                load(args.review),
                args.workspace,
                args.out,
                input_path=args.input,
            )
        else:
            backend = (
                demo_backend
                if args.command == "demo"
                else partial(claude_backend, auth_mode=args.auth_mode)
            )
            passed = run_loop(
                data,
                args.out,
                backend,
                args.max_iterations,
                workspace=args.workspace,
                input_path=args.input,
            )
        print("PASS (document quality gate)" if passed else "FAIL (see evaluation.json)")
        return 0 if passed else 1
    except subprocess.TimeoutExpired:
        print("External model timed out; extraction did not approve any claims.", file=sys.stderr)
        return 2
    except (OSError, ValueError, RuntimeError, ValidationError) as error:
        print(f"Input/configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
