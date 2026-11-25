"""Harness evolution: recurring failures -> hypothesis -> isolated candidate -> paired comparison -> proposal.

Nothing here applies a change. The furthest it goes is a local review report; publishing
needs a recorded human approval and passes an allowlist and secret scan first.
"""

import json
import math
import re
import statistics
from pathlib import Path

from . import agents, loop
from .evaluation import run_command
from .guard import git
from .redact import find_deny_terms, find_secrets, load_deny_terms
from .store import (
    HarnessError,
    append_log,
    canonical,
    iso,
    private_dir,
    read_json,
    read_log,
    require_human,
    sha256,
    validate,
    write_json,
    write_private,
)

LOG_ID = "evolution"
BLOCKING = ("critical", "high")
# +1: higher is better, -1: lower is better.
DIRECTIONS = {
    "first_pass_rate": 1,
    "quality": 1,
    "failure_recurrence_rate": -1,
    "fix_count": -1,
    "duration_seconds": -1,
    "tokens": -1,
    "cost": -1,
}
CAUSE_HINTS = {
    "decision:guard_regression": "skill",
    "decision:required_regression": "skill",
    "decision:side_effect": "skill",
    "decision:below_noise": "instruction",
    "decision:criteria_tamper": "instruction",
    "decision:workspace": "instruction",
    "decision:unevaluable": "evaluation",
    "decision:agent_downgrade": "evaluation",
    "audit:evaluation": "evaluation",
    "audit:evidence": "input",
    "audit:premise": "input",
}
DEFAULT_CRITERIA = {"min_cases_per_split": 2, "min_trials": 3, "noise_k": 2.0}


def root(home):
    return Path(home) / "evolution"


def ledger_path(home):
    return root(home) / "ledger.jsonl"


def _append(home, kind, data, now):
    return append_log(ledger_path(home), LOG_ID, kind, data, now)


def fold(events):
    state = {"suite": None, "hypotheses": {}, "proposals": {}}
    for event in events:
        kind, data, seq = event["type"], event["data"], event["seq"]
        hypothesis = state["hypotheses"].get(data.get("hypothesis_id"))
        if kind == "suite_approved":
            state["suite"] = data
        elif kind == "evo_hypothesis_registered":
            state["hypotheses"][data["hypothesis_id"]] = {**data, "stage": "registered"}
        elif kind == "candidate_frozen":
            hypothesis.update(stage="frozen", candidate=data)
        elif kind == "comparison_recorded":
            hypothesis.update(stage="compared", comparison={**data, "seq": seq})
        elif kind == "evo_audit_recorded":
            findings = {f["id"]: {**f, "status": "open"} for f in data["findings"]}
            hypothesis.update(stage="audited", audit={**data, "findings": findings, "seq": seq})
        elif kind == "evo_finding_resolved":
            hypothesis["audit"]["findings"][data["finding_id"]].update(
                status=data["status"], resolution=data["evidence"]
            )
        elif kind == "proposal_created":
            hypothesis.update(stage="proposed", proposal_id=data["proposal_id"])
            state["proposals"][data["proposal_id"]] = {**data, "status": "awaiting_approval"}
        elif kind == "proposal_approved":
            state["proposals"][data["proposal_id"]].update(
                status="approved", approval=data["approval"]
            )
        elif kind == "evo_closed":
            hypothesis.update(stage="closed", outcome=data["outcome"], closed_reason=data["reason"])
    return state


def load(home):
    return fold(read_log(ledger_path(home)))


# --- suite: the frozen evaluation cases, holdout split and improvement criteria -----------------


def load_suite(path):
    path = Path(path).expanduser().resolve()
    if not path.is_file():
        raise HarnessError(f"evaluation suite not found: {path}")
    suite = validate("evolution#suite", json.loads(path.read_text(encoding="utf-8")))
    if suite["criteria"]["metric"] not in DIRECTIONS:
        raise HarnessError(f"unknown criteria metric: {suite['criteria']['metric']}")
    base, files = path.parent, {}
    for relative in suite["runner"].get("files", []) + [
        c["path"] for c in suite["cases"] if "path" in c
    ]:
        target = (base / relative).resolve()
        if not target.is_relative_to(base):
            raise HarnessError(f"suite files must stay inside the suite directory: {relative}")
        if not target.is_file():
            raise HarnessError(f"suite file is missing: {relative}")
        files[relative] = sha256(target.read_bytes())
    digest = sha256(canonical({"suite": suite, "files": files}))
    repo = (base / suite["subject"]["repo"]).resolve()
    top = git(repo, "rev-parse", "--show-toplevel", check=False).strip()
    if top and base.is_relative_to(Path(top).resolve()):
        raise HarnessError(
            "the suite must be kept outside the subject repository; "
            "inside it, a candidate could read the holdout cases and the runner"
        )
    suite["subject"]["repo"] = str(repo)
    return suite, digest


def suite_approve(home, path, reviewer, reason, now):
    """Cases, the holdout split and the improvement criteria change only with a person's approval."""
    approval = require_human(reviewer, reason)
    suite, digest = load_suite(path)
    for split in ("train", "holdout"):
        if not any(case["split"] == split for case in suite["cases"]):
            raise HarnessError(f"suite has no {split} case")
    data = {
        "path": str(Path(path).expanduser().resolve()),
        "suite_hash": digest,
        "approval": approval,
    }
    _append(home, "suite_approved", data, now)
    return digest


def approved_suite(state):
    """The suite as approved. An edited suite file is refused, not silently re-read."""
    if not state["suite"]:
        raise HarnessError(
            "no approved evaluation suite; a person runs `evolve suite-approve` first"
        )
    suite, digest = load_suite(state["suite"]["path"])
    if digest != state["suite"]["suite_hash"]:
        raise HarnessError(
            "the evaluation suite changed since it was approved; it needs a new approval"
        )
    return suite, Path(state["suite"]["path"]).parent


# --- analysis: which failures recur --------------------------------------------------------------


def occurrences(home):
    """Every recorded failure as (id, source, classification, signature). Ids are stable, so
    reading the same record again is harmless. Hook records come from real work: internal."""
    for run_id in loop.list_runs(home):
        try:
            state = loop.load(home, run_id)
        except HarnessError:
            continue  # a corrupt ledger is reported by `status`; it is not evidence
        classification = state["spec"].get("data_classification", "internal")
        for decision in state["decisions"]:
            for reason in decision["reasons"] if decision["verdict"] != "adopt" else []:
                signature = "decision:" + reason.split(":")[0]
                yield f"{run_id}:{decision['seq']}:{signature}", run_id, classification, signature
        for audit in state["audits"]:
            for finding in audit["findings"].values():
                if finding["status"] == "confirmed":
                    occurrence_id = f"{run_id}:{audit['audit_id']}:{finding['id']}"
                    yield occurrence_id, run_id, classification, f"audit:{finding['category']}"
    events = Path(home) / "events.jsonl"
    for line in events.read_text(encoding="utf-8").splitlines() if events.exists() else []:
        event = json.loads(line)
        if event["kind"] == "tool_failure":
            signature = f"tool_failure:{event.get('tool_name', 'unknown')}"
            yield event["id"], event["project"], "internal", signature


def _fresh(pattern):
    return {key: v for key, v in pattern["occurrences"].items() if key not in pattern["cited"]}


def analyze(home, config):
    """Fold failures into patterns. One failure, or one source, stays a candidate and is not acted on."""
    settings = config["evolution"]
    path = root(home) / "patterns.json"
    patterns = read_json(path, {})
    for occurrence_id, source, classification, signature in occurrences(home):
        pattern = patterns.setdefault(
            signature, {"signature": signature, "occurrences": {}, "cited": []}
        )
        pattern["occurrences"].setdefault(
            occurrence_id, {"source": source, "classification": classification}
        )
    for pattern in patterns.values():
        fresh = _fresh(pattern)
        count, sources = len(fresh), len({item["source"] for item in fresh.values()})
        recurring = (
            count >= settings["min_occurrences"] and sources >= settings["min_distinct_sources"]
        )
        strong = (
            recurring
            and count >= 2 * settings["min_occurrences"]
            and sources > settings["min_distinct_sources"]
        )
        base = (
            pattern["signature"]
            if pattern["signature"] in CAUSE_HINTS
            else pattern["signature"].split(":")[0]
        )
        pattern.update(
            fresh=count,
            sources=sources,
            confidence="strong" if strong else "recurring" if recurring else "candidate",
            cause_hint=CAUSE_HINTS.get(base, "tool" if base == "tool_failure" else "skill"),
            classification=agents.strictest(
                "public", *(item["classification"] for item in fresh.values())
            ),
        )
    write_json(path, patterns)
    return patterns


def _cite(home, signature, evidence):
    """Mark evidence as used, so a pattern is not proposed again without new failures."""
    path = root(home) / "patterns.json"
    patterns = read_json(path, {})
    patterns[signature]["cited"] = sorted({*patterns[signature]["cited"], *evidence})
    write_json(path, patterns)


def eligible(patterns):
    ready = [p for p in patterns.values() if p["confidence"] != "candidate"]
    return sorted(ready, key=lambda p: (-p["fresh"], p["signature"]))


# --- hypothesis and candidate --------------------------------------------------------------------


def hypothesis_add(home, hypothesis, now):
    """Register the improvement hypothesis and pin the current harness version as the baseline."""
    validate("evolution#hypothesis", hypothesis)
    state = load(home)
    suite, _ = approved_suite(state)
    patterns = read_json(root(home) / "patterns.json", {})
    pattern = patterns.get(hypothesis["pattern"])
    if not pattern or pattern["confidence"] == "candidate":
        raise HarnessError(
            "the pattern does not recur across enough records and sources; a single failure is not generalized"
        )
    holdout = {case["id"] for case in suite["cases"] if case["split"] == "holdout"}
    text = canonical(hypothesis)
    leaked = sorted(
        case_id
        for case_id in holdout
        if re.search(rf"(?<![\w-]){re.escape(case_id)}(?![\w-])", text)
    )
    if leaked:
        raise HarnessError(f"hypothesis cites holdout cases: {', '.join(leaked)}")
    hypothesis_id = f"EH-{len(state['hypotheses']) + 1:04d}"
    data = {
        "hypothesis_id": hypothesis_id,
        **hypothesis,
        "evidence": sorted(_fresh(pattern)),
        "classification": agents.strictest(
            suite.get("data_classification", "internal"), pattern["classification"]
        ),
        "baseline_sha": git(
            suite["subject"]["repo"], "rev-parse", suite["subject"]["baseline_ref"]
        ).strip(),
        "suite_hash": state["suite"]["suite_hash"],
    }
    _append(home, "evo_hypothesis_registered", data, now)
    _cite(home, hypothesis["pattern"], data["evidence"])  # only once the hypothesis exists
    return hypothesis_id


def _hypothesis(state, hypothesis_id, *stages):
    hypothesis = state["hypotheses"].get(hypothesis_id)
    if not hypothesis:
        raise HarnessError(f"unknown evolution hypothesis: {hypothesis_id}")
    if stages and hypothesis["stage"] not in stages:
        raise HarnessError(
            f"{hypothesis_id} is at stage {hypothesis['stage']}, expected {' or '.join(stages)}"
        )
    return hypothesis


def _worktree(home, repo, name, sha):
    """A detached worktree of the subject repo inside the state home. The user's checkout is untouched."""
    path = private_dir(root(home) / "worktrees") / name
    if not path.exists():
        git(repo, "worktree", "add", "--detach", str(path), sha)
        # Remember where the work tree points. The agent that edits it can rewrite `.git`.
        write_private(_link_record(path), (path / ".git").read_text(encoding="utf-8"))
    return path


def _link_record(worktree):
    return worktree.parent.parent / "worktree-links" / worktree.name


def _assert_link(worktree):
    """Refuse to run git in a work tree whose `.git` no longer points where we created it.

    A repointed link would make git read a config of the editor's choosing, and git config
    can name programs to run.
    """
    link, record = worktree / ".git", _link_record(worktree)
    intact = (
        link.is_file()
        and not link.is_symlink()
        and record.is_file()
        and link.read_text(encoding="utf-8") == record.read_text(encoding="utf-8")
    )
    if not intact:
        raise HarnessError(
            "the git link of the candidate worktree was altered; discard the candidate"
        )


def candidate_create(home, hypothesis_id):
    state = load(home)
    hypothesis = _hypothesis(state, hypothesis_id, "registered")
    suite, _ = approved_suite(state)
    return _worktree(home, suite["subject"]["repo"], hypothesis_id, hypothesis["baseline_sha"])


def _candidate_patch(worktree, baseline_sha):
    _assert_link(worktree)
    git(worktree, "add", "-A")
    return git(worktree, "diff", "--cached", "--binary", "--no-renames", baseline_sha)


def candidate_freeze(home, hypothesis_id, now):
    state = load(home)
    hypothesis = _hypothesis(state, hypothesis_id, "registered")
    worktree = root(home) / "worktrees" / hypothesis_id
    if not worktree.is_dir():
        raise HarnessError(f"no candidate worktree for {hypothesis_id}; run candidate-create first")
    patch = _candidate_patch(worktree, hypothesis["baseline_sha"])
    if not patch.strip():
        raise HarnessError("the candidate does not differ from the baseline")
    if find_secrets(patch):
        raise HarnessError(
            "the candidate diff contains a secret-shaped string; remove it before freezing"
        )
    # -z: exact names. --no-renames: a move lists the path it leaves as well as the one it creates.
    listing = git(
        worktree,
        "diff",
        "--cached",
        "--no-renames",
        "--name-only",
        "-z",
        hypothesis["baseline_sha"],
    )
    files = sorted(name for name in listing.split("\0") if name)
    relative = f"candidates/{hypothesis_id}.patch"
    write_private(root(home) / relative, patch)
    data = {
        "hypothesis_id": hypothesis_id,
        "patch": relative,
        "sha256": sha256(patch),
        "files": files,
    }
    _append(home, "candidate_frozen", data, now)
    return data


# --- comparison ----------------------------------------------------------------------------------


def _trial(suite, suite_dir, version, version_dir, case, index):
    env = {
        "AI_HARNESS_VERSION_DIR": str(version_dir),
        "AI_HARNESS_CASE_ID": case["id"],
        "AI_HARNESS_CASE_PATH": str(suite_dir / case["path"]) if "path" in case else "",
        "AI_HARNESS_TRIAL": str(index),
    }
    # The runner works on content a candidate wrote: it gets the adapter environment, not the user's.
    result = run_command(
        suite["runner"]["argv"],
        suite_dir,
        suite["runner"].get("timeout_seconds", 600),
        env=env,
        minimal_env=True,
    )
    trial = {
        "version": version,
        "case_id": case["id"],
        "split": case["split"],
        "origin": case["origin"],
        "trial": index,
        "duration_seconds": result["duration_seconds"],
    }
    if result["exit_code"] != 0:
        return {**trial, "error": f"runner exit code {result['exit_code']}"}
    try:
        return {**trial, **validate("evolution#trial_output", json.loads(result["stdout"]))}
    except (ValueError, HarnessError) as error:
        return {**trial, "error": f"runner output is invalid: {str(error)[:200]}"}


def _samples(trials, version, split, metric, count):
    """One value per trial round, so the spread reflects run-to-run variation."""
    values = []
    for index in range(count):
        rows = [
            t for t in trials if (t["version"], t["split"], t["trial"]) == (version, split, index)
        ]
        if metric == "first_pass_rate":
            picked = [float(row["passed"]) for row in rows]
        elif metric == "failure_recurrence_rate":
            picked = [float(not row["passed"]) for row in rows if row["origin"] == "past_failure"]
        else:
            picked = [row[metric] for row in rows if metric in row]
        if picked:
            values.append(statistics.fmean(picked))
    return values


def _stat(values):
    if not values:
        return None
    return {
        "mean": statistics.fmean(values),
        "stddev": statistics.stdev(values) if len(values) > 1 else None,
        "n": len(values),
    }


def summarize(suite, trials):
    """Aggregate both versions and apply the improvement rule that was fixed before the run."""
    criteria = {**DEFAULT_CRITERIA, **suite["criteria"]}
    count = suite["trials"]
    summary = {"metrics": {}, "trials": count, "criteria": criteria, "notes": []}

    def verdict(value, *reasons):
        return {**summary, "verdict": value, "reasons": list(reasons)}

    errors = sorted(
        {f"{t['version']}/{t['case_id']}: {t['error']}" for t in trials if "error" in t}
    )
    if errors:
        return verdict("hold", "runner_error", *errors)
    metrics = {
        version: {
            split: {
                name: _stat(_samples(trials, version, split, name, count)) for name in DIRECTIONS
            }
            for split in ("train", "holdout")
        }
        for version in ("baseline", "candidate")
    }
    summary["metrics"] = metrics
    regressions = set()
    for case in suite["cases"]:
        rows = {
            v: [t for t in trials if (t["version"], t["case_id"]) == (v, case["id"])]
            for v in metrics
        }
        checks = {"passed", *(name for row in rows["baseline"] for name in row.get("required", {}))}
        for check in checks:
            before = [
                row["passed"] if check == "passed" else row["required"].get(check)
                for row in rows["baseline"]
            ]
            after = [
                row["passed"] if check == "passed" else row.get("required", {}).get(check)
                for row in rows["candidate"]
            ]
            if all(value is True for value in before) and not all(value is True for value in after):
                regressions.add(f"regression:{case['id']}:{check}")
    if regressions:
        return verdict("reject", *sorted(regressions))
    thin = [
        f"insufficient_sample:{split}_cases"
        for split in ("train", "holdout")
        if sum(case["split"] == split for case in suite["cases"]) < criteria["min_cases_per_split"]
    ]
    if count < criteria["min_trials"]:
        thin.append("insufficient_sample:trials")
    name, direction = criteria["metric"], DIRECTIONS[criteria["metric"]]
    gains = {}
    for split in ("train", "holdout"):
        before, after = metrics["baseline"][split][name], metrics["candidate"][split][name]
        if not before or not after:
            return verdict("hold", f"metric_not_reported:{name}:{split}", *thin)
        noise = criteria["noise_k"] * math.sqrt(
            sum((side["stddev"] or 0.0) ** 2 / side["n"] for side in (before, after))
        )
        gains[split] = {"gain": (after["mean"] - before["mean"]) * direction, "noise": noise}
        if before["stddev"] is None:
            summary["notes"].append(f"{split}: one trial, so the spread is unknown")
    summary["gains"] = gains
    threshold = max(criteria["min_delta"], gains["holdout"]["noise"])
    for split in ("holdout", "train"):
        if gains[split]["gain"] < -max(gains[split]["noise"], 1e-12):
            return verdict("reject", f"{split}_worse")
    if thin:
        return verdict("hold", *thin)
    if gains["holdout"]["gain"] <= 0 or gains["holdout"]["gain"] < threshold:
        return verdict("hold", "holdout_gain_below_threshold")
    return verdict("propose")


def compare(home, hypothesis_id, now, dry_run=False, should_cancel=lambda: False):
    """Run every case on the pinned baseline and the frozen candidate, interleaved, same conditions."""
    state = load(home)
    hypothesis = _hypothesis(state, hypothesis_id, "frozen")
    suite, suite_dir = approved_suite(state)
    if hypothesis["suite_hash"] != state["suite"]["suite_hash"]:
        raise HarnessError(
            "the suite was re-approved after this hypothesis was registered; register a new one"
        )
    sha = hypothesis["baseline_sha"]
    candidate = root(home) / "worktrees" / hypothesis_id
    if not candidate.exists():  # removed since the freeze: rebuild it from the frozen patch
        _worktree(home, suite["subject"]["repo"], hypothesis_id, sha)
        git(
            candidate,
            "apply",
            "--index",
            "--binary",
            str(root(home) / hypothesis["candidate"]["patch"]),
        )
    if sha256(_candidate_patch(candidate, sha)) != hypothesis["candidate"]["sha256"]:
        raise HarnessError("the candidate changed after it was frozen")
    if dry_run and not suite.get("dry_run_safe"):
        raise agents.WouldRun(
            f"would run {suite['trials']} trials x {len(suite['cases'])} cases x 2 versions"
        )
    versions = {
        "baseline": _worktree(home, suite["subject"]["repo"], f"baseline-{sha[:12]}", sha),
        "candidate": candidate,
    }
    trials = []
    for index in range(suite["trials"]):
        for case in suite["cases"]:
            order = ("baseline", "candidate") if index % 2 == 0 else ("candidate", "baseline")
            for version in order:
                if should_cancel():
                    raise HarnessError("comparison cancelled")
                trials.append(_trial(suite, suite_dir, version, versions[version], case, index))
    summary = summarize(suite, trials)
    relative = f"comparisons/{hypothesis_id}.json"
    write_json(root(home) / relative, trials)
    data = {
        "hypothesis_id": hypothesis_id,
        "trials_file": relative,
        "suite_hash": hypothesis["suite_hash"],
        **summary,
    }
    _append(home, "comparison_recorded", data, now)
    return data


# --- audit, proposal, approval, publish gate -----------------------------------------------------


def audit_record(home, hypothesis_id, audit, independence, now):
    validate("audit", audit)
    if independence not in (*loop.INDEPENDENT, "same_context"):
        raise HarnessError("independence must be separate_process, subagent or same_context")
    _hypothesis(load(home), hypothesis_id, "compared", "audited")
    data = {"hypothesis_id": hypothesis_id, "independence": independence, **audit}
    _append(home, "evo_audit_recorded", data, now)


def audit_resolve(home, hypothesis_id, finding_id, status, evidence, now):
    hypothesis = _hypothesis(load(home), hypothesis_id, "audited")
    if finding_id not in hypothesis["audit"]["findings"]:
        raise HarnessError(f"unknown finding: {finding_id}")
    if status not in loop.RESOLUTIONS or not (evidence or "").strip():
        raise HarnessError(
            "a finding is resolved as confirmed, refuted or unverifiable, with evidence"
        )
    data = {
        "hypothesis_id": hypothesis_id,
        "finding_id": finding_id,
        "status": status,
        "evidence": evidence,
    }
    _append(home, "evo_finding_resolved", data, now)


def close(home, hypothesis_id, outcome, reason, now):
    _hypothesis(load(home), hypothesis_id, "registered", "frozen", "compared", "audited")
    _append(
        home,
        "evo_closed",
        {"hypothesis_id": hypothesis_id, "outcome": outcome, "reason": reason},
        now,
    )


def _open_findings(hypothesis):
    return [
        f["id"]
        for f in hypothesis["audit"]["findings"].values()
        if f["severity"] in BLOCKING and f["status"] != "refuted"
    ]


def _fmt(stat):
    if not stat:
        return "-"
    spread = "±?" if stat["stddev"] is None else f"±{stat['stddev']:.3g}"
    return f"{stat['mean']:.3g} {spread} (n={stat['n']})"


def _report(proposal):
    comparison, hypothesis = proposal["measurements"], proposal["hypothesis"]
    lines = [
        f"# Harness 改善提案 {proposal['id']}",
        "",
        "この提案はローカルのレビュー用です。承認を記録するまで公開用ブランチも PR も作りません。",
        "",
        "## 仮説",
        "",
        f"- 対象パターン: `{hypothesis['pattern']}`（原因の分類: {hypothesis['cause']}）",
        f"- 仮説: {hypothesis['statement']}",
        f"- 反証条件: {hypothesis['falsification']}",
        f"- 根拠にした記録: {len(hypothesis['evidence'])} 件",
        "",
        "## 差分",
        "",
        f"- 基準の版: `{proposal['baseline_sha']}`",
        f"- patch: `{proposal['diff']['patch']}`（sha256 `{proposal['diff']['sha256']}`）",
        *[f"- `{path}`" for path in proposal["diff"]["files"]],
        "",
        "## 測定結果",
        "",
        f"試行 {comparison['trials']} 回。事前に決めた改善条件: holdout の {comparison['criteria']['metric']} が "
        f"{comparison['criteria']['min_delta']} 以上、かつ測定誤差（k={comparison['criteria']['noise_k']}）を超えること。",
        "",
        "| 指標 | 分割 | 現行版 | 候補版 |",
        "|---|---|---|---|",
    ]
    for name in DIRECTIONS:
        for split in ("train", "holdout"):
            before = comparison["metrics"]["baseline"][split][name]
            after = comparison["metrics"]["candidate"][split][name]
            if before or after:
                lines.append(f"| {name} | {split} | {_fmt(before)} | {_fmt(after)} |")
    lines += ["", *[f"- 注意: {note}" for note in comparison["notes"]]]
    lines += ["", "## 独立監査", "", f"- 独立性: {proposal['audit']['independence']}"]
    for finding in proposal["audit"]["findings"]:
        lines.append(
            f"- {finding['id']} [{finding['severity']}/{finding['status']}] {finding['claim']}"
        )
    lines += [f"- 限界: {limit}" for limit in proposal["audit"]["limitations"]]
    lines += ["", "## リスク", "", *[f"- {risk}" for risk in proposal["risks"]]]
    lines += [
        "",
        "## ロールバック",
        "",
        *[f"{n}. {step}" for n, step in enumerate(proposal["rollback"], 1)],
    ]
    return "\n".join(lines) + "\n"


def propose(home, hypothesis_id, now):
    """Write the local review report. Requires the predefined gain and an independent audit."""
    state = load(home)
    hypothesis = _hypothesis(state, hypothesis_id, "audited")
    comparison, audit = hypothesis["comparison"], hypothesis["audit"]
    if comparison["verdict"] != "propose":
        raise HarnessError(
            f"comparison verdict is {comparison['verdict']}: {', '.join(comparison['reasons'])}"
        )
    if audit["seq"] < comparison["seq"] or audit["independence"] not in loop.INDEPENDENT:
        raise HarnessError("an independent audit of this comparison is required before proposing")
    if _open_findings(hypothesis):
        raise HarnessError(
            f"audit findings are not refuted: {', '.join(_open_findings(hypothesis))}"
        )
    proposal_id = f"P-{len(state['proposals']) + 1:04d}"
    folder = root(home) / "proposals" / proposal_id
    patch = (root(home) / hypothesis["candidate"]["patch"]).read_text(encoding="utf-8")
    write_private(folder / "candidate.patch", patch)
    proposal = {
        "schema_version": 1,
        "id": proposal_id,
        "hypothesis_id": hypothesis_id,
        "created_at": iso(now),
        "status": "awaiting_approval",
        "hypothesis": {
            key: hypothesis[key]
            for key in ("pattern", "cause", "statement", "falsification", "evidence")
        },
        "baseline_sha": hypothesis["baseline_sha"],
        "diff": {**hypothesis["candidate"], "patch": f"proposals/{proposal_id}/candidate.patch"},
        "measurements": {
            key: comparison[key]
            for key in ("metrics", "trials", "criteria", "gains", "notes", "verdict")
        },
        "audit": {
            "independence": audit["independence"],
            "findings": list(audit["findings"].values()),
            "limitations": audit["limitations"],
        },
        "risks": hypothesis.get("risks", [])
        + ["評価ケースの外の作業で同じ改善が出るかは未確認", *comparison["notes"]]
        + [
            f"`{path}` は評価・承認・監査の仕組み自体を変更する。通常の変更より慎重に読むこと"
            for path in hypothesis["candidate"]["files"]
            if SELF_GOVERNING.search(path)
        ],
        "rollback": [
            "公開前: この提案を承認しなければ何も変わらない。候補の worktree は `evolve cleanup` で消せる。",
            "公開後・マージ前: PR を閉じ、公開用ブランチを削除する。",
            f"マージ後: マージコミットを `git revert` する。基準の版は `{hypothesis['baseline_sha']}`。",
        ],
    }
    validate("proposal", proposal)
    write_json(folder / "proposal.json", proposal)
    write_private(folder / "report.md", _report(proposal))
    _append(
        home, "proposal_created", {"proposal_id": proposal_id, "hypothesis_id": hypothesis_id}, now
    )
    return {"proposal_id": proposal_id, "report": str(folder / "report.md")}


def approve(home, proposal_id, reviewer, reason, now):
    approval = require_human(reviewer, reason)
    proposal = load(home)["proposals"].get(proposal_id)
    if not proposal or proposal["status"] != "awaiting_approval":
        raise HarnessError(f"{proposal_id} is not awaiting approval")
    _append(home, "proposal_approved", {"proposal_id": proposal_id, "approval": approval}, now)


# The harness's own gates: runtime, schemas and the auditor definition.
SELF_GOVERNING = re.compile(
    r"autonomous-improvement/(?:runtime|schemas)/|agents/improvement-auditor\.md$"
)
PATCH_HEADER = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$', re.MULTILINE)
UNINSPECTABLE = {
    r"^(?:new file|deleted file|old|new) mode 120000$": "symbolic link in the patch",
    r"^(?:old|new) mode \d+$": "file mode change in the patch",
    r"^new file mode 100755$": "executable file in the patch",
    r"^GIT binary patch$|^Binary files ": "binary content cannot be inspected",
}


def _glob(pattern):
    """Path glob where `*` and `?` stay inside one path segment and `**` crosses segments."""
    parts = re.split(r"(\*\*|\*|\?)", pattern)
    table = {"**": ".*", "*": "[^/]*", "?": "[^/]"}
    return re.compile("".join(table.get(part, re.escape(part)) for part in parts))


def publish_check(home, config, proposal_id):
    """The gate in front of a public branch: approved, allowlisted paths, no secrets, no unknown data."""
    state = load(home)
    record = state["proposals"].get(proposal_id)
    if not record:
        raise HarnessError(f"unknown proposal: {proposal_id}")
    if record["status"] != "approved":
        raise HarnessError(
            f"{proposal_id} has no recorded human approval; nothing may be published"
        )
    folder = root(home) / "proposals" / proposal_id
    proposal = read_json(folder / "proposal.json")
    patch = (folder / "candidate.patch").read_text(encoding="utf-8")
    files = proposal["diff"]["files"]
    blocks = []
    if sha256(patch) != proposal["diff"]["sha256"]:
        blocks.append("the patch differs from the one that was measured and approved")
    in_patch = sorted({name for pair in PATCH_HEADER.findall(patch) for name in pair})
    if in_patch != sorted(files):
        blocks.append("the patch touches files the proposal does not list")
    for path in files:
        if not any(_glob(pattern).fullmatch(path) for pattern in config["publish"]["allow_paths"]):
            blocks.append(f"path is not on the publish allowlist: {path}")
    # Unknown data is blocked: anything a reviewer cannot read as text in the diff.
    for marker, problem in UNINSPECTABLE.items():
        if re.search(marker, patch, re.MULTILINE):
            blocks.append(problem)
    added = "\n".join([*files, *(line[1:] for line in patch.splitlines() if line.startswith("+"))])
    blocks += [f"secret-shaped string: {name}" for name in find_secrets(added)]
    terms = load_deny_terms(config["privacy"].get("deny_terms_file"))
    blocks += [
        f"forbidden term #{terms.index(term) + 1} from the deny list"
        for term in find_deny_terms(added, terms)
    ]
    if blocks:
        raise HarnessError("publish blocked: " + "; ".join(blocks))
    branch = f"harness-evolution/{proposal_id.lower()}"
    return {
        "proposal_id": proposal_id,
        "approved_by": record["approval"]["reviewer"],
        "patch": str(folder / "candidate.patch"),
        "commands": [
            f"git switch -c {branch} {proposal['baseline_sha']}",
            f"git apply --index {folder / 'candidate.patch'}",
            "git commit  # 人が内容を確認してから",
            f"git push -u origin {branch} && gh pr create  # マージは別の承認",
        ],
    }


def cleanup(home):
    """Remove every worktree this harness created in the subject repository."""
    removed = []
    folder = root(home) / "worktrees"
    if folder.is_symlink() or not folder.is_dir():
        return removed
    for path in sorted(folder.iterdir()):
        if path.is_symlink() or not path.is_dir():
            continue  # a link would lead `git worktree remove --force` to someone else's work tree
        common = git(
            path, "rev-parse", "--path-format=absolute", "--git-common-dir", check=False
        ).strip()
        if common:
            git(Path(common).parent, "worktree", "remove", "--force", str(path), check=False)
            removed.append(path.name)
    return removed


# --- the daily batch -----------------------------------------------------------------------------


def daily(home, config, now, dry_run=False, should_cancel=lambda: False, budget=None):
    """One evolution pass. Resumes an unfinished hypothesis; without recurring evidence it does nothing."""
    cost = 0.0
    patterns = analyze(home, config)

    def result(action, **extra):
        return {"action": action, "cost": cost, **extra}

    for _ in range(8):  # one hypothesis moves at most a handful of stages per pass
        if should_cancel():
            return result("cancelled")
        state = load(home)
        active = [
            h for h in state["hypotheses"].values() if h["stage"] not in ("proposed", "closed")
        ]
        if not active:
            ready = eligible(patterns)
            if not ready:
                return result(
                    "none", reason="no failure pattern recurs often enough across sources"
                )
            if not state["suite"]:
                return result("needs_human", reason="no approved evaluation suite")
            suite, _ = approved_suite(state)
            pattern = ready[0]
            payload = {
                "pattern": {
                    key: pattern[key]
                    for key in ("signature", "fresh", "sources", "confidence", "cause_hint")
                },
                "train_cases": [c["id"] for c in suite["cases"] if c["split"] == "train"],
            }
            output, spent = agents.invoke(
                config,
                "hypothesize",
                payload,
                root(home),
                agents.strictest(
                    suite.get("data_classification", "internal"), pattern["classification"]
                ),
                dry_run,
                budget,
            )
            cost += spent
            if "abstain" in output:
                _cite(home, pattern["signature"], _fresh(pattern))
                return result("none", reason=f"no hypothesis: {output['abstain']}")
            hypothesis_add(home, {"pattern": pattern["signature"], **output}, now)
            patterns = analyze(home, config)
            continue
        hypothesis, hypothesis_id = active[0], active[0]["hypothesis_id"]
        suite, _ = approved_suite(state)
        classification = hypothesis["classification"]
        if hypothesis["stage"] == "registered":
            worktree = candidate_create(home, hypothesis_id)
            payload = {
                key: hypothesis[key] for key in ("pattern", "cause", "statement", "falsification")
            }
            _, spent = agents.invoke(
                config, "candidate", payload, worktree, classification, dry_run, budget
            )
            cost += spent
            if not _candidate_patch(worktree, hypothesis["baseline_sha"]).strip():
                close(home, hypothesis_id, "abandoned", "the candidate made no change", now)
                return result(
                    "none", reason="the candidate made no change", hypothesis_id=hypothesis_id
                )
            candidate_freeze(home, hypothesis_id, now)
        elif hypothesis["stage"] == "frozen":
            compare(home, hypothesis_id, now, dry_run, should_cancel)
        elif hypothesis["comparison"]["verdict"] != "propose":
            comparison = hypothesis["comparison"]
            outcome = "rejected" if comparison["verdict"] == "reject" else "held"
            close(home, hypothesis_id, outcome, ", ".join(comparison["reasons"]), now)
            return result(outcome, hypothesis_id=hypothesis_id, reasons=comparison["reasons"])
        elif hypothesis["stage"] == "compared":
            comparison = hypothesis["comparison"]
            payload = {
                "hypothesis": {
                    key: hypothesis[key]
                    for key in ("pattern", "cause", "statement", "falsification")
                },
                "comparison": {
                    key: comparison[key]
                    for key in ("metrics", "gains", "criteria", "trials", "notes")
                },
                "diff_files": hypothesis["candidate"]["files"],
            }
            audit, spent = agents.invoke(
                config, "auditor", payload, root(home), classification, dry_run, budget
            )
            cost += spent
            audit_record(home, hypothesis_id, audit, "separate_process", now)
        elif hypothesis["stage"] == "audited":
            if _open_findings(hypothesis):
                return result(
                    "needs_human",
                    reason="audit findings need verification",
                    hypothesis_id=hypothesis_id,
                )
            return result(
                "proposed", hypothesis_id=hypothesis_id, **propose(home, hypothesis_id, now)
            )
    return result("needs_human", reason="evolution pass did not converge")
