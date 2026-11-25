"""`sre-harness iac validate`. The only IaC command: there is none that applies, deploys,
starts a build or moves a change to another state."""

import json
from pathlib import Path

from .. import paths
from . import validate as validate_stage


def _validate(args, env, out):
    root = paths.state_root(env)
    delivery = Path(args.delivery) if args.delivery is not None else None
    check, risk_record = validate_stage.validate(
        root, Path(args.plan), Path(args.evidence), delivery, env
    )
    if args.format == "evaluator-output":
        value = validate_stage.evaluator_output(check, risk_record)
    else:
        value = validate_stage.summary(check, risk_record)
    print(json.dumps(value, indent=2, ensure_ascii=False), file=out)
    return validate_stage.exit_code(check)


def register(groups):
    iac = groups.add_parser(
        "iac", help="IaC delivery contract: check records and classify plan risk (no apply)"
    )
    stages = iac.add_subparsers(dest="stage", required=True)
    validate = stages.add_parser(
        "validate", help="check a delivery record and its evidence; classify a plan's risk"
    )
    validate.add_argument("--plan", required=True, help="plan JSON (`terraform show -json` shape)")
    validate.add_argument(
        "--evidence", required=True, help="directory the delivery's evidence locators point into"
    )
    validate.add_argument("--delivery", help="delivery record JSON (schema iac-delivery)")
    validate.add_argument(
        "--format",
        choices=["summary", "evaluator-output"],
        default="summary",
        help="evaluator-output prints the v2 evaluator adapter JSON on stdout",
    )
    validate.set_defaults(handler=_validate)
