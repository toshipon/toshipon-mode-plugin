"""`sre-harness <group> <stage>`. Each stage reads the previous stage's files from a run."""

import argparse
import json
import sys

from . import __version__, paths
from .errors import EXIT_INPUT, EXIT_OK, HarnessError


def _runs_list(args, env, out):
    root = paths.state_root(env)
    print(json.dumps({"root": str(root), "runs": paths.list_runs(root)}, indent=2), file=out)
    return EXIT_OK


def _register_runs(groups):
    runs = groups.add_parser("runs", help="run directories kept outside the repository")
    stages = runs.add_subparsers(dest="stage", required=True)
    listing = stages.add_parser("list", help="print the state root and the run ids")
    listing.set_defaults(handler=_runs_list)


REGISTRARS = [_register_runs]


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(EXIT_INPUT, f"error: {message}\n")


def build_parser():
    parser = _Parser(prog="sre-harness", description=__doc__)
    parser.add_argument("--version", action="version", version=__version__)
    groups = parser.add_subparsers(dest="group", required=True, parser_class=_Parser)
    for register in REGISTRARS:
        register(groups)
    return parser


def main(argv=None, env=None, out=None):
    """Exit codes: 0 ok, 1 quality gate or incomplete, 2 input or configuration, 3 refused."""
    out = out or sys.stdout
    try:
        args = build_parser().parse_args(argv)
        return args.handler(args, env, out)
    except HarnessError as error:
        print(f"error: {error}", file=sys.stderr)
        return error.exit_code
    except SystemExit as stop:
        return stop.code if isinstance(stop.code, int) else EXIT_INPUT
