"""Command-line entry point for workspace generation and evaluation."""

import argparse
import json
import subprocess
import sys
from functools import partial
from pathlib import Path

from jsonschema.exceptions import ValidationError

from .claims import (
    MAX_EXTRACT_JOBS,
    MAX_EXTRACTION_BYTES,
    MIN_BATCH_BYTES,
    REJECTED_NAME,
    extract_claims,
    prepare_input,
    review_claim,
)
from .context_cli import assess, write_impact
from .intake import collect
from .relations import relate_claims
from .runner import (
    DEFAULT_TIMEOUT_SECONDS,
    MAX_TIMEOUT_SECONDS,
    MIN_TIMEOUT_SECONDS,
    claude_backend,
    demo_backend,
    evaluate_candidate,
    run_loop,
)


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


def bounded_int(low, high, unit=""):
    def parse(text):
        value = int(text)
        if not low <= value <= high:
            raise argparse.ArgumentTypeError(f"must be {low}-{high}{unit}")
        return value

    return parse


timeout_seconds = bounded_int(MIN_TIMEOUT_SECONDS, MAX_TIMEOUT_SECONDS, " seconds")


def model_options(child):
    child.add_argument("--auth-mode", choices=["api-key", "subscription"], default="api-key")
    child.add_argument(
        "--timeout-seconds",
        type=timeout_seconds,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Timeout for each Claude CLI call. Large source sets need more than the default.",
    )


def model_backend(args):
    return partial(claude_backend, auth_mode=args.auth_mode, timeout=args.timeout_seconds)


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
                model_options(child)
    child = sub.add_parser("collect")
    child.add_argument("--manifest", required=True)
    child.add_argument("--out", required=True)
    child = sub.add_parser("extract")
    child.add_argument("--workspace", required=True)
    model_options(child)
    child.add_argument(
        "--batch-bytes",
        type=bounded_int(MIN_BATCH_BYTES, MAX_EXTRACTION_BYTES, " bytes"),
        help="Split whole sources into model calls of at most this many bytes.",
    )
    child.add_argument(
        "--jobs",
        type=bounded_int(1, MAX_EXTRACT_JOBS),
        default=1,
        help="Number of batches extracted in parallel.",
    )
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
    child = sub.add_parser("relate")
    child.add_argument("--workspace", required=True)
    child.add_argument("--out", required=True)
    model_options(child)
    child.add_argument(
        "--proposals",
        help="Validate candidate relations from a local review instead of calling Claude.",
    )
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
            backend = model_backend(args)
            if args.proposals:
                proposals = load(args.proposals)

                def backend(role, payload, output_schema):
                    return proposals

            result = extract_claims(
                args.workspace, backend, batch_bytes=args.batch_bytes, jobs=args.jobs
            )
            print(f"Extracted {len(result['claims'])} claims. Explicit review is required.")
            rejected_path = Path(args.workspace) / REJECTED_NAME
            if rejected_path.exists():
                count = len(load(rejected_path)["rejected"])
                print(
                    f"Dropped {count} claims whose quotation, hash or source did not match; "
                    f"see {rejected_path}.",
                    file=sys.stderr,
                )
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
        if args.command == "relate":
            backend = model_backend(args)
            if args.proposals:
                proposals = load(args.proposals)

                def backend(role, payload, output_schema):
                    return proposals

            result = relate_claims(args.workspace, backend, args.out)
            print(
                f"Listed {len(result['relations'])} relation candidates. They do not change assess."
            )
            return 0
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
            backend = demo_backend if args.command == "demo" else model_backend(args)
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
        print(
            "External model timed out; nothing was approved. Retry with a larger --timeout-seconds "
            "or fewer sources.",
            file=sys.stderr,
        )
        return 2
    except (OSError, ValueError, RuntimeError, ValidationError) as error:
        print(f"Input/configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
