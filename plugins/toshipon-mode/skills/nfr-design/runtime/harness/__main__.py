"""Command-line entry point for workspace generation and evaluation."""

import argparse
import json
import sys
from functools import partial
from pathlib import Path

from jsonschema.exceptions import ValidationError

from .runner import claude_backend, demo_backend, evaluate_candidate, run_loop


def load(path):
    def reject(value):
        raise ValueError(f"Non-finite JSON number: {value}")

    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=reject)


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
    args = parser.parse_args()
    try:
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
    except (OSError, ValueError, ValidationError) as error:
        print(f"Input/configuration error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
