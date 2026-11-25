"""The plan risk classification: the bundled Rego sample, run by opa through the guard."""

import json
import re
from types import SimpleNamespace

import pytest

from examples.synthetic import iac_scenarios as sc
from sre_harness import guard, records
from sre_harness.errors import QualityGateError
from sre_harness.iac import risk
from tests.iac_helpers import no_opa, opa_on_path  # noqa: F401  (fixtures)

# Built-ins that reach the network, the environment, the clock or randomness. The bundled
# capabilities file must leave every one of them out, so a policy calling them does not compile.
UNSAFE_BUILTINS = {
    "http.send",
    "net.lookup_ip_addr",
    "opa.runtime",
    "time.now_ns",
    "rand.intn",
    "uuid.rfc4122",
    "trace",
    "print",
    "internal.print",
}


def _plan(*changes):
    return {"format_version": "1.2", "resource_changes": list(changes)}


def _rc(address, kind, actions, before=None, after=None):
    return {
        "address": address,
        "type": kind,
        "change": {"actions": actions, "before": before, "after": after},
    }


# The bundled files, checked without opa -------------------------------------------------


def test_the_capabilities_file_lists_no_network_or_environment_builtin():
    capabilities = json.loads(guard.OPA_CAPABILITIES.read_text(encoding="utf-8"))
    names = {item["name"] for item in capabilities["builtins"]}
    assert names and not names & UNSAFE_BUILTINS
    assert not any(name.startswith(("http.", "net.lookup", "opa.", "io.")) for name in names)
    assert capabilities["allow_net"] == []


def test_the_policy_source_calls_no_network_builtin():
    source = guard.OPA_POLICY.read_text(encoding="utf-8")
    code = "\n".join(line.split("#", 1)[0] for line in source.splitlines())
    for name in UNSAFE_BUILTINS:
        assert not re.search(rf"\b{re.escape(name)}\s*\(", code), name


def test_the_policy_and_capabilities_are_bundled_next_to_the_package():
    assert guard.OPA_POLICY.is_file() and guard.OPA_POLICY.parent.name == "policies"
    assert guard.OPA_CAPABILITIES.parent == guard.OPA_POLICY.parent
    assert guard.OPA_QUERY == "data.sre_harness.iac_risk.result"


# Parsing what opa answers, without opa --------------------------------------------------


def _answer(monkeypatch, returncode, stdout):
    seen = []

    def run(argv, **options):
        seen.append(SimpleNamespace(argv=argv, **options))
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr="noise")

    monkeypatch.setattr(guard, "_run", run)
    monkeypatch.setattr(guard, "_executable", lambda name: f"/usr/bin/{name}")
    return seen


def _opa_json(value):
    return json.dumps({"result": [{"expressions": [{"value": value, "text": "q"}]}]})


def test_an_opa_answer_becomes_an_evaluated_risk_record(monkeypatch):
    value = {
        "level": "high",
        "changes": [
            {
                "address": "aws_sqs_queue.legacy",
                "type": "aws_sqs_queue",
                "actions": ["delete"],
                "level": "high",
                "reasons": ["delete"],
            }
        ],
    }
    seen = _answer(monkeypatch, 0, _opa_json(value))
    plan = _plan(_rc("aws_sqs_queue.legacy", "aws_sqs_queue", ["delete"], {"name": "q"}))
    found = risk.classify(plan)
    assert found["status"] == "evaluated" and found["level"] == "high"
    assert found["changes"] == value["changes"]
    assert found["engine"]["policy_sha256"] == records.sha256(guard.OPA_POLICY.read_bytes())
    assert json.loads(seen[0].input) == plan


@pytest.mark.parametrize(
    "returncode, stdout",
    [
        (1, '{"errors": [{"message": "x"}]}'),
        (0, "not json"),
        (0, "{}"),  # undefined: the policy gave no result
        (0, _opa_json({"level": "extreme", "changes": []})),
        (0, _opa_json({"level": "low"})),
    ],
)
def test_an_opa_answer_out_of_shape_is_a_quality_failure(monkeypatch, returncode, stdout):
    _answer(monkeypatch, returncode, stdout)
    with pytest.raises(QualityGateError):
        risk.classify(_plan())


def test_without_opa_the_risk_is_unevaluable_and_nothing_is_guessed(no_opa):  # noqa: F811
    found = risk.classify(sc.high_risk_plan())
    assert found["status"] == "unevaluable"
    assert "opa" in found["unevaluable_reason"]
    assert "level" not in found and "changes" not in found


# The Rego sample, run by the real opa ---------------------------------------------------


@pytest.mark.parametrize("kind", ["low", "high"])
def test_the_sample_plans_get_the_expected_levels(opa_on_path, kind):  # noqa: F811
    found = risk.classify(sc.PLANS[kind]())
    expected = sc.EXPECTED_RISK[kind]
    assert found["status"] == "evaluated"
    assert found["level"] == expected["level"]
    assert {item["address"]: item["level"] for item in found["changes"]} == expected["changes"]
    assert records.violations("iac-delivery", found, "risk_result") == []


@pytest.mark.parametrize(
    "change, level, reason",
    [
        (_rc("aws_sqs_queue.a", "aws_sqs_queue", ["delete"], {"name": "a"}), "high", "delete"),
        (_rc("aws_instance.a", "aws_instance", ["delete", "create"], {}, {}), "high", "replace"),
        (_rc("aws_instance.a", "aws_instance", ["create", "delete"], {}, {}), "high", "replace"),
        (
            _rc("aws_iam_role.a", "aws_iam_role", ["update"], {"x": 1}, {"x": 2}),
            "high",
            "sensitive_type",
        ),
        (
            _rc("aws_security_group.a", "aws_security_group", ["create"], None, {"name": "a"}),
            "high",
            "sensitive_type",
        ),
        (
            _rc(
                "aws_s3_bucket.a",
                "aws_s3_bucket",
                ["update"],
                {"acl": "private"},
                {"acl": "public-read"},
            ),
            "high",
            "public_setting",
        ),
        (
            _rc(
                "aws_db_instance.a",
                "aws_db_instance",
                ["update"],
                {},
                {"publicly_accessible": True},
            ),
            "high",
            "public_setting",
        ),
        (
            _rc("aws_s3_bucket.a", "aws_s3_bucket", ["update"], {"tags": {}}, {"tags": {"a": "b"}}),
            "low",
            "tags_only",
        ),
        (
            _rc("aws_sqs_queue.a", "aws_sqs_queue", ["create"], None, {"name": "a"}),
            "medium",
            "create",
        ),
        (
            _rc("aws_sqs_queue.a", "aws_sqs_queue", ["update"], {"n": 1}, {"n": 2}),
            "medium",
            "update",
        ),
        (_rc("aws_iam_role.a", "aws_iam_role", ["no-op"], {"x": 1}, {"x": 1}), "none", None),
        (_rc("aws_sqs_queue.a", "aws_sqs_queue", ["forget"], {}, {}), "medium", "unknown_action"),
    ],
)
def test_each_rule_of_the_sample(opa_on_path, change, level, reason):  # noqa: F811
    found = risk.classify(_plan(change))
    (item,) = found["changes"]
    assert item["level"] == level
    assert (reason in item["reasons"]) if reason else item["reasons"] == []
    assert found["level"] == level


def test_an_empty_plan_has_no_risk(opa_on_path):  # noqa: F811
    found = risk.classify(_plan())
    assert found["level"] == "none" and found["changes"] == []
