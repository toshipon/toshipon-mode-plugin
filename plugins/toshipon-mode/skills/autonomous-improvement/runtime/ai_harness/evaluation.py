"""Evaluator adapters, measurement statistics and the deterministic adopt/reject rule."""

import json
import math
import operator
import os
import statistics
import subprocess
import time
from pathlib import Path

from .redact import redact_text
from .store import ACTIVE_ENV, HarnessError, canonical, iso, sha256, validate

OPS = {
    "<=": operator.le,
    "<": operator.lt,
    ">=": operator.ge,
    ">": operator.gt,
    "==": operator.eq,
}
TAIL = 2000
DEFAULT_TIMEOUT = 600
DEFAULT_NOISE_K = 2.0
# Options that switch off or loosen permission prompts. No command the harness starts may carry them.
FORBIDDEN_ARGS = ("dangerously-skip-permissions", "bypassPermissions")
# A direct `claude` command line may not widen its own permissions either.
FORBIDDEN_CLAUDE_OPTIONS = (
    "--settings",
    "--allowedTools",
    "--allowed-tools",
    "--permission-prompt-tool",
)
# What an agent adapter inherits. Evaluators and guard checks are the user's own commands and keep theirs.
ADAPTER_ENV = ("PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TMPDIR", "TERM")
ADAPTER_ENV_PREFIXES = ("LC_", "ANTHROPIC_", "CLAUDE_", "AI_HARNESS_")


def assert_safe_argv(argv):
    parts = [str(part) for part in argv]
    if any(marker in part for part in parts for marker in FORBIDDEN_ARGS):
        raise HarnessError("refusing a command that bypasses permission prompts")
    if not parts or Path(parts[0]).name != "claude":
        return
    for index, part in enumerate(parts):
        option, _, value = part.partition("=")
        value = value or (parts[index + 1] if index + 1 < len(parts) else "")
        if option in FORBIDDEN_CLAUDE_OPTIONS or (
            option == "--permission-mode" and value == "auto"
        ):
            raise HarnessError(
                f"refusing a claude command that loosens permission checks: {option}"
            )


def _adapter_env():
    return {
        key: value
        for key, value in os.environ.items()
        if key in ADAPTER_ENV or key.startswith(ADAPTER_ENV_PREFIXES)
    }


def run_command(argv, cwd, timeout=DEFAULT_TIMEOUT, stdin=None, env=None, minimal_env=False):
    """Run an adapter as argv (never a shell). Children carry the hook-recursion guard."""
    assert_safe_argv(argv)
    inherited = _adapter_env() if minimal_env else os.environ
    child_env = {**inherited, **(env or {}), ACTIVE_ENV: "1"}
    started = time.monotonic()
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=child_env,
        )
        code, out, err = done.returncode, done.stdout, done.stderr
    except subprocess.TimeoutExpired:
        code, out, err = None, "", f"timed out after {timeout}s"
    except OSError as error:
        code, out, err = None, "", f"cannot start: {error.strerror}"
    return {
        "exit_code": code,
        "stdout": out,
        "stderr_tail": redact_text(err[-TAIL:]),
        "duration_seconds": round(time.monotonic() - started, 3),
    }


def criteria_hash(spec, project):
    """Freeze what decides pass/fail: metrics, guard checks, acceptance and evaluator files."""
    files = {}
    for relative in spec["evaluation"].get("files", []):
        target = Path(project) / relative
        if not target.is_file():
            raise HarnessError(f"evaluator file is missing: {relative}")
        files[relative] = sha256(target.read_bytes())
    frozen = {key: spec.get(key) for key in ("evaluation", "guard", "acceptance")}
    return sha256(canonical({"spec": frozen, "files": files}))


def measure(spec, project, evaluation_id, purpose, now):
    """Run the evaluator `repeats` times and fold the samples into one stored evaluation."""
    config = spec["evaluation"]
    declared = {metric["name"]: metric for metric in config["metrics"]}
    samples = {name: [] for name in declared}
    rationales, evidence, runs, reason = {}, [], [], None
    for _ in range(config.get("repeats", 1)):
        result = run_command(
            config["command"], project, config.get("timeout_seconds", DEFAULT_TIMEOUT)
        )
        runs.append({key: result[key] for key in ("exit_code", "duration_seconds", "stderr_tail")})
        if result["exit_code"] != 0:
            reason = f"evaluator exit code {result['exit_code']}"
            break
        try:
            output = validate("evaluation#output", json.loads(result["stdout"]))
        except (ValueError, HarnessError) as error:
            reason = f"evaluator output is invalid: {str(error)[:200]}"
            break
        if output.get("unevaluable"):
            reason = output["unevaluable"]
            break
        evidence.extend(output.get("evidence", []))
        for name, metric in declared.items():
            value = output["metrics"].get(name)
            if value is None:
                reason = f"evaluator did not report metric {name}"
                break
            if isinstance(value, dict):
                samples[name].extend(value.get("samples") or [value["value"]])
                if value.get("rationale"):
                    rationales[name] = value["rationale"]
            else:
                samples[name].append(value)
            if metric["kind"] == "llm" and name not in rationales:
                reason = f"LLM metric {name} has no rationale"
                break
        if reason:
            break
    record = {
        "id": evaluation_id,
        "purpose": purpose,
        "status": "unevaluable" if reason else "evaluated",
        "unevaluable_reason": reason,
        "metrics": {},
        "required": [],
        "evidence": evidence,
        "runs": runs,
        "started_at": iso(now),
    }
    if reason:
        return record
    for name, metric in declared.items():
        values = samples[name]
        record["metrics"][name] = {
            "value": statistics.fmean(values),
            "n": len(values),
            "stddev": statistics.stdev(values) if len(values) > 1 else None,
            "kind": metric["kind"],
            "role": metric["role"],
            "rationale": rationales.get(name),
        }
        if metric["role"] == "required":
            passed = OPS[metric["op"]](record["metrics"][name]["value"], metric["threshold"])
            record["required"].append({"metric": name, "passed": bool(passed)})
    return record


def effect_threshold(metric, reference, candidate, noise_k):
    """Smallest absolute change that counts: the declared effect, the noise floor, or k standard errors.

    min_effect is relative to the reference. A reference of zero has no relative scale, so there
    it is read as an absolute change rather than silently becoming zero.
    """
    variance = sum(
        (side["stddev"] or 0.0) ** 2 / side["n"] for side in (reference, candidate) if side["n"]
    )
    return max(
        metric.get("min_effect", 0.0) * (abs(reference["value"]) or 1.0),
        metric.get("noise_floor", 0.0),
        noise_k * math.sqrt(variance),
    )


def compare(spec, reference, candidate):
    """Per scored metric: signed gain in the good direction against its effect threshold."""
    noise_k = spec["evaluation"].get("noise_k", DEFAULT_NOISE_K)
    measurements = {}
    for metric in spec["evaluation"]["metrics"]:
        if metric["role"] != "scored":
            continue
        before, after = reference["metrics"][metric["name"]], candidate["metrics"][metric["name"]]
        gain = after["value"] - before["value"]
        if metric["direction"] == "lower":
            gain = -gain
        threshold = effect_threshold(metric, before, after, noise_k)
        measurements[metric["name"]] = {
            "reference": before["value"],
            "value": after["value"],
            "gain": gain,
            "threshold": threshold,
            "kind": metric["kind"],
            "effect": "improved"
            if gain > threshold
            else "worsened"
            if gain < -threshold
            else "none",
        }
    return measurements


def failed_names(items, key):
    return {item[key] for item in items if not item["passed"]}


def judge(spec, reference, evaluation, checks, violations):
    """The whole decision rule. Returns (verdict, reason codes, measurements)."""
    kinds = {violation["kind"] for violation in violations}
    if kinds & {"preexisting_modified", "head_moved"}:
        return "needs_human", sorted(f"workspace:{kind}" for kind in kinds), {}
    if kinds & {"protected_path", "criteria_changed"}:
        return "reject", ["criteria_tamper"], {}
    if evaluation["status"] == "skipped":
        return "reject", ["no_change"], {}
    if evaluation["status"] != "evaluated":
        return "unevaluable", ["unevaluable"], {}
    reasons = [
        f"guard_regression:{name}"
        for name in sorted(failed_names(checks, "name") - failed_names(reference["guard"], "name"))
    ]
    reasons += [
        f"required_regression:{name}"
        for name in sorted(
            failed_names(evaluation["required"], "metric")
            - failed_names(reference["evaluation"]["required"], "metric")
        )
    ]
    measurements = compare(spec, reference["evaluation"], evaluation)
    reasons += [
        f"side_effect:{name}" for name, m in measurements.items() if m["effect"] == "worsened"
    ]
    if reasons:
        return "reject", reasons, measurements
    # An LLM score may veto (above) but never carries an adoption on its own.
    if any(
        m["effect"] == "improved" and m["kind"] == "deterministic" for m in measurements.values()
    ):
        return "adopt", [], measurements
    return "reject", ["below_noise"], measurements


def accepted(spec, best):
    """Acceptance: every guard check and required metric passes and every target is met."""
    evaluation = best["evaluation"]
    if failed_names(best["guard"], "name") or failed_names(evaluation["required"], "metric"):
        return False
    return all(
        OPS[target["op"]](evaluation["metrics"][target["metric"]]["value"], target["value"])
        for target in spec.get("acceptance", {}).get("targets", [])
    )
