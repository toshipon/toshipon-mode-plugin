"""Plan risk classification: the bundled Rego sample, evaluated by opa through the guard.

There is one implementation of the rules, the Rego file. Without opa the risk is recorded as
`unevaluable`; it is not worked out another way in Python.
"""

import json

from .. import guard, records
from ..errors import QualityGateError

TIMEOUT = 60


def _engine():
    return {
        "name": "opa",
        "policy_sha256": records.sha256(guard.OPA_POLICY.read_bytes()),
        "capabilities_sha256": records.sha256(guard.OPA_CAPABILITIES.read_bytes()),
        "query": guard.OPA_QUERY,
    }


def _value(code, stdout):
    if code != 0:
        raise QualityGateError(f"opa eval failed with exit code {code}.")
    try:
        answer = json.loads(stdout)
        value = answer["result"][0]["expressions"][0]["value"]
    except (TypeError, ValueError, KeyError, IndexError):
        raise QualityGateError("opa gave no result for the bundled query.") from None
    problems = records.violations("iac-delivery", value, "risk_result")
    if problems:
        raise QualityGateError("opa answered out of shape: " + "; ".join(problems[:3]))
    return value


def classify(plan, env=None):
    """{status, engine, level, changes} or {status: unevaluable, engine, unevaluable_reason}."""
    engine = _engine()
    try:
        code, stdout = guard.opa_risk(records.canonical(plan), timeout=TIMEOUT, env=env)
    except guard.OpaMissing:
        return {
            "status": "unevaluable",
            "engine": engine,
            "unevaluable_reason": (
                "opa is not on PATH; the Rego risk sample was not run and no level is given."
            ),
        }
    value = _value(code, stdout)
    return {
        "status": "evaluated",
        "engine": engine,
        "level": value["level"],
        "changes": value["changes"],
    }
