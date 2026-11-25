"""Synthetic IaC delivery records, plans and evidence files for `iac validate`.

Everything is made up: the change (`CHG-0001`), the people (`carol` requests, `alice`
implements, `bob` holds the privileged role), the resources and the account id
(`000000000000`). Nothing is copied from a real organisation, account or service.

    case = write_case(tmp_path / "case")                  # a valid delivery up to `deployed`
    case = write_case(tmp_path / "case", "approver_is_implementer")
    plan = write_plan(tmp_path / "plan.json", "low")      # a plan alone, no delivery

`write_case` returns a dict with `plan`, `evidence` and `delivery` paths. The plan given to
`--plan` is the one staged, approved and deployed. Each name in `BROKEN` writes the same case
with one fault, and the violation code the check must report:

- `approver_is_implementer`: the privileged approval is given by the implementer.
- `approver_not_privileged`: the approver's role is not `privileged`.
- `skipped_stage`: `implemented` goes straight to `pr_opened`, without staging.
- `deployed_other_plan`: the execution record names a plan other than the approved one.
- `evidence_hash_mismatch`: the test result changed after its hash was recorded.
- `time_went_back`: the approval is dated before the PR was opened.

Run as a script to write every case: `python -m examples.synthetic.iac_scenarios OUT_DIR`
from `runtime/`.
"""

import argparse
import hashlib
import json
from pathlib import Path

CHANGE_ID = "CHG-0001"
REQUESTER = {"name": "carol", "role": "requester"}
IMPLEMENTER = {"name": "alice", "role": "engineer"}
APPROVER = {"name": "bob", "role": "privileged"}
ROLE_ARN = "arn:aws:iam::000000000000:role/example-app"

STAGES = (
    "requirement_approved",
    "designed",
    "implemented",
    "staging_verified",
    "pr_opened",
    "privileged_approved",
    "deployed",
)
TIMES = (
    "2026-01-05T09:00:00Z",
    "2026-01-06T10:00:00Z",
    "2026-01-08T15:30:00Z",
    "2026-01-09T11:00:00Z",
    "2026-01-09T13:00:00Z",
    "2026-01-12T09:30:00Z",
    "2026-01-12T14:00:00Z",
)

BROKEN = {
    "approver_is_implementer": "approver_is_implementer",
    "approver_not_privileged": "approver_not_privileged",
    "skipped_stage": "skipped_stage",
    "deployed_other_plan": "plan_hash_mismatch",
    "evidence_hash_mismatch": "evidence_hash_mismatch",
    "time_went_back": "time_went_back",
}


def _change(address, kind, actions, before, after):
    return {
        "address": address,
        "mode": "managed",
        "type": kind,
        "name": address.split(".", 1)[1],
        "provider_name": "registry.terraform.io/hashicorp/aws",
        "change": {"actions": actions, "before": before, "after": after, "after_unknown": {}},
    }


def low_risk_plan():
    """Only tags change, and one resource stays as it is."""
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "resource_changes": [
            _change(
                "aws_s3_bucket.logs",
                "aws_s3_bucket",
                ["update"],
                {"bucket": "example-logs", "tags": {"team": "sre"}},
                {"bucket": "example-logs", "tags": {"team": "sre", "cost": "shared"}},
            ),
            _change(
                "aws_sqs_queue.events",
                "aws_sqs_queue",
                ["no-op"],
                {"name": "example-events"},
                {"name": "example-events"},
            ),
        ],
    }


def high_risk_plan():
    """A delete, a replacement, an IAM change, a security group and a public flag."""
    return {
        "format_version": "1.2",
        "terraform_version": "1.9.0",
        "resource_changes": [
            _change(
                "aws_sqs_queue.legacy",
                "aws_sqs_queue",
                ["delete"],
                {"name": "example-legacy"},
                None,
            ),
            _change(
                "aws_instance.web",
                "aws_instance",
                ["delete", "create"],
                {"ami": "ami-00000000", "instance_type": "t3.small"},
                {"ami": "ami-11111111", "instance_type": "t3.small"},
            ),
            _change(
                "aws_iam_role_policy.app",
                "aws_iam_role_policy",
                ["update"],
                {"role": ROLE_ARN, "policy": '{"Statement":[]}'},
                {"role": ROLE_ARN, "policy": '{"Statement":[{"Effect":"Allow"}]}'},
            ),
            _change(
                "aws_security_group_rule.ingress",
                "aws_security_group_rule",
                ["create"],
                None,
                {"type": "ingress", "from_port": 443, "to_port": 443},
            ),
            _change(
                "aws_db_instance.main",
                "aws_db_instance",
                ["update"],
                {"identifier": "example-db", "publicly_accessible": False},
                {"identifier": "example-db", "publicly_accessible": True},
            ),
            _change(
                "aws_sqs_queue.jobs",
                "aws_sqs_queue",
                ["create"],
                None,
                {"name": "example-jobs"},
            ),
        ],
    }


PLANS = {"low": low_risk_plan, "high": high_risk_plan}

# The levels `runtime/policies/iac_risk.rego` gives each change of the plans above.
EXPECTED_RISK = {
    "low": {
        "level": "low",
        "changes": {"aws_s3_bucket.logs": "low", "aws_sqs_queue.events": "none"},
    },
    "high": {
        "level": "high",
        "changes": {
            "aws_sqs_queue.legacy": "high",
            "aws_instance.web": "high",
            "aws_iam_role_policy.app": "high",
            "aws_security_group_rule.ingress": "high",
            "aws_db_instance.main": "high",
            "aws_sqs_queue.jobs": "medium",
        },
    },
}


def _dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def write_plan(path, kind="high"):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump(PLANS[kind]()), encoding="utf-8")
    return path


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_evidence(evidence_dir, plan_kind="high"):
    """Evidence files for every stage. Returns {kind: locator}."""
    evidence_dir = Path(evidence_dir)
    files = {
        "requirement": (
            "requirement/REQ-0001.json",
            _dump({"id": "REQ-0001", "status": "approved", "title": "ログの保持期間を延ばす"}),
        ),
        "design": ("design/design.md", "# 設計（合成）\n\n保持期間を 400 日にする。\n"),
        "commit": ("implementation/commit.txt", "1" * 40 + "\n"),
        "plan": ("staging/plan.json", _dump(PLANS[plan_kind]())),
        "test_result": ("staging/test-result.json", _dump({"passed": 12, "failed": 0})),
        "pull_request": ("pr/pull-request.json", _dump({"repo": "example-org/demo", "number": 42})),
        "note": ("notes/rejection.md", "差し戻し（合成）。\n"),
    }
    for locator, text in files.values():
        target = evidence_dir / locator
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    plan_hash = sha256_file(evidence_dir / files["plan"][0])
    execution = ("deploy/execution.json", _dump({"plan_sha256": plan_hash, "result": "succeeded"}))
    (evidence_dir / execution[0]).parent.mkdir(parents=True, exist_ok=True)
    (evidence_dir / execution[0]).write_text(execution[1], encoding="utf-8")
    locators = {kind: locator for kind, (locator, _) in files.items()}
    locators["execution_record"] = execution[0]
    return locators


STAGE_EVIDENCE = {
    "requirement_approved": ("requirement",),
    "designed": ("requirement", "design"),
    "implemented": ("commit",),
    "staging_verified": ("plan", "test_result"),
    "pr_opened": ("pull_request",),
    "privileged_approved": ("plan",),
    "deployed": ("execution_record",),
}
STAGE_ACTOR = {
    "requirement_approved": REQUESTER,
    "designed": IMPLEMENTER,
    "implemented": IMPLEMENTER,
    "staging_verified": IMPLEMENTER,
    "pr_opened": IMPLEMENTER,
    "privileged_approved": APPROVER,
    "deployed": APPROVER,
}


def evidence_item(evidence_dir, kind, locator):
    return {"kind": kind, "sha256": sha256_file(Path(evidence_dir) / locator), "locator": locator}


def valid_delivery(evidence_dir, plan_kind="high", until="deployed"):
    """A delivery that follows the contract up to `until`, with its evidence written."""
    locators = write_evidence(evidence_dir, plan_kind)
    transitions, previous = [], None
    for stage, at in zip(STAGES, TIMES):
        transitions.append(
            {
                "from": previous,
                "to": stage,
                "at": at,
                "actor": dict(STAGE_ACTOR[stage]),
                "evidence": [
                    evidence_item(evidence_dir, kind, locators[kind])
                    for kind in STAGE_EVIDENCE[stage]
                ],
            }
        )
        previous = stage
        if stage == until:
            break
    return {"schema_version": 1, "change_id": CHANGE_ID, "state": until, "transitions": transitions}


def _index(delivery, stage):
    return next(i for i, item in enumerate(delivery["transitions"]) if item["to"] == stage)


def broken_delivery(evidence_dir, fault, plan_kind="high"):
    """The valid delivery with the one fault `BROKEN` names."""
    delivery = valid_delivery(evidence_dir, plan_kind)
    transitions = delivery["transitions"]
    approval = transitions[_index(delivery, "privileged_approved")]
    if fault == "approver_is_implementer":
        approval["actor"] = {"name": IMPLEMENTER["name"], "role": "privileged"}
    elif fault == "approver_not_privileged":
        approval["actor"] = {"name": "dave", "role": "engineer"}
    elif fault == "skipped_stage":
        staging = _index(delivery, "staging_verified")
        del transitions[staging]
        transitions[staging]["from"] = "implemented"
    elif fault == "deployed_other_plan":
        other = Path(evidence_dir) / "deploy/execution-other.json"
        other.write_text(_dump({"plan_sha256": "0" * 64, "result": "succeeded"}), encoding="utf-8")
        deployed = transitions[_index(delivery, "deployed")]
        deployed["evidence"] = [
            evidence_item(evidence_dir, "execution_record", "deploy/execution-other.json")
        ]
    elif fault == "evidence_hash_mismatch":
        (Path(evidence_dir) / "staging/test-result.json").write_text(
            _dump({"passed": 11, "failed": 1}), encoding="utf-8"
        )
    elif fault == "time_went_back":
        approval["at"] = "2026-01-09T12:00:00Z"
    else:
        raise ValueError(f"unknown fault {fault!r}; choose from {sorted(BROKEN)}")
    return delivery


def write_case(directory, fault=None, plan_kind="high"):
    """Write plan.json, evidence/ and delivery.json under `directory`. Returns their paths."""
    directory = Path(directory)
    evidence = directory / "evidence"
    if fault is None:
        delivery = valid_delivery(evidence, plan_kind)
    else:
        delivery = broken_delivery(evidence, fault, plan_kind)
    plan = directory / "plan.json"
    plan.write_bytes((evidence / "staging/plan.json").read_bytes())
    path = directory / "delivery.json"
    path.write_text(_dump(delivery), encoding="utf-8")
    return {"plan": plan, "evidence": evidence, "delivery": path}


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("out_dir")
    args = parser.parse_args()
    out = Path(args.out_dir)
    print(json.dumps({k: str(v) for k, v in write_case(out / "valid").items()}))
    for fault in BROKEN:
        print(json.dumps({k: str(v) for k, v in write_case(out / fault, fault).items()}))
    print(write_plan(out / "plan-low.json", "low"))


if __name__ == "__main__":
    main()
