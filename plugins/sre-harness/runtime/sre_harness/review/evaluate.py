"""`review evaluate`: apply candidates to the evaluation-side PRs of a split and measure them.

1. Holdout: the split's hash must hold, analyze and generate must have run with this split, and
   no lesson may refer to an evaluation PR. Otherwise exit 2.
2. Units: hunks of the evaluation PRs as first written. A hunk a reviewer commented on is the
   thread's `diff_hunk` (the code the reviewer saw, before the fix). Files no thread touched
   contribute the hunks of their patch. A unit's id is a hash of PR, path and hunk.
3. Rules: approved registry entries of this repository (the baseline) and this run's
   candidates. Declarative checks run in the interpreter (`apply_rules`); prose rules are
   applied by `review-eval-judge` (a backend; `stub` is fixed rules). The judge only says which
   hunks a rule flags: labels, metrics and status are computed here.
4. Labels per hunk and rule (`metrics`), adjudication applied, then counts, metrics and PR-level
   bootstrap for the baseline, baseline + all candidates, and baseline + each candidate.
   With fewer than `min_prs` evaluation PRs no metric value is given (`unevaluable`).
5. `evaluation.json`, `evaluation.md`; evaluated candidates move to `evaluated` with the
   criteria hash. A body edited since generate becomes a new version first. When the result is
   `unevaluable`, candidates stay (or go back to) `proposed`, without a criteria hash, and the
   history entry notes why; such a candidate cannot be approved.
"""

import time
from pathlib import Path

from .. import llm, paths, records
from ..errors import BoundaryError, InputError, QualityGateError
from . import apply_rules, candidates, classify, fix, metrics, registry
from .markdown import code_block, escape

STAGE = "evaluate"
JUDGE_SCHEMA = "llm-review-judge"
JUDGE_AGENT = Path(__file__).resolve().parents[3] / "agents" / "review-eval-judge.md"
MAX_JUDGE_BATCH_BYTES = 300 * 1024
MAX_ADJUDICATION_BYTES = 16 * 1024 * 1024
TIMINGS = "timings.jsonl"
FORMATS = ("summary", "evaluator-output")

JUDGE_TASK = """\
Task: apply the one review rule in DATA to each hunk in DATA. List in flags every hunk the rule
would flag, with the rule id, the hunk id and a one-sentence rationale that points at the line.
Leave out hunks the rule does not flag. Use only the ids in DATA.

The instructions of the review-eval-judge agent follow."""

timer = time.monotonic  # tests may replace it


def judge_instructions():
    text = JUDGE_AGENT.read_text(encoding="utf-8")
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[1]
    return f"{JUDGE_TASK}\n\n{text.strip()}"


# Holdout ----------------------------------------------------------------------------------


def check_holdout(run_dir, split):
    evaluation_prs = set(split["evaluation"]["prs"])
    for name in ("analyze.json", "generate.json"):
        stage = records.read_json(paths.child(run_dir, name))
        if stage.get("split_sha256") != split["sha256"]:
            raise InputError(
                f"{name.split('.')[0]} did not run with this split (--split); its output may use "
                "events from the evaluation period."
            )
    lessons = records.read_jsonl(paths.child(run_dir, "lessons.jsonl"))
    referenced = sorted({number for lesson in lessons for number in lesson["prs"]} & evaluation_prs)
    if referenced:
        raise InputError(
            f"lessons.jsonl refers to evaluation PR(s) {referenced}; generation must not see the "
            "evaluation side."
        )


# Units ------------------------------------------------------------------------------------


def build_units(run_dir, split):
    """(units, evaluation threads with fix, threads left without a hunk)."""
    chosen = set(split["evaluation"]["prs"])
    threads = [
        thread
        for thread in records.read_jsonl(paths.child(run_dir, "threads.jsonl"))
        if thread["pr"]["number"] in chosen and thread["kind"] == "inline_thread"
    ]
    contexts = fix.pull_contexts(run_dir)
    linked = fix.attach(run_dir, threads, contexts)
    found, commented, unplaced = {}, set(), []

    def unit(number, path, hunk, source):
        key = records.sha256(f"{number}\n{path}\n{hunk}")
        if key not in found:
            added, new = apply_rules.sides(hunk)
            found[key] = {
                "pr": number,
                "path": path,
                "hunk_sha256": key,
                "source": source,
                "thread_ids": [],
                "hunk": hunk,
                "added": added,
                "new": new,
            }
        return found[key]

    for thread in sorted(linked, key=lambda t: (t["pr"]["number"], t["id"])):
        detail = thread["thread"]
        path, hunk = detail.get("path"), detail.get("diff_hunk")
        if not isinstance(path, str) or not isinstance(hunk, str):
            unplaced.append(thread["id"])
            continue
        if not apply_rules.HUNK_HEADER.match(hunk):
            unplaced.append(thread["id"])
            continue
        commented.add((thread["pr"]["number"], path))
        unit(thread["pr"]["number"], path, hunk, "thread")["thread_ids"].append(thread["id"])
    for number in sorted(chosen):
        for path, patch in sorted(contexts.get(number, fix.NO_PULL)["files"].items()):
            if (number, path) in commented or not patch:
                continue
            for hunk in apply_rules.hunks(patch):
                unit(number, path, hunk, "patch")
    units = sorted(found.values(), key=lambda u: (u["pr"], u["path"], u["source"], u["hunk"]))
    for number, item in enumerate(units, start=1):
        item["id"] = records.record_id("HU", number)
    return units, {thread["id"]: thread for thread in linked}, unplaced


# Labels -----------------------------------------------------------------------------------


def same_kind(text, title):
    words = classify._tokens(title)
    shared = words & classify._tokens(text or "")
    return bool(words) and len(shared) >= min(3, len(words)) and 2 * len(shared) >= len(words)


def label_cells(units, threads, rules, adjudication):
    """{(unit id, rule id): label}, plus the cells a person adjudicated invalid."""
    cells, invalid = {}, set()
    for unit in units:
        for rule in rules:
            cell = (unit["id"], rule["id"])
            raised = [
                threads[t]
                for t in unit["thread_ids"]
                if threads[t]["comments"]
                and same_kind(threads[t]["comments"][0].get("body"), rule["title"])
            ]
            verdict = adjudication.get((unit["hunk_sha256"], rule["id"]))
            statuses = {(thread.get("fix") or {}).get("status") for thread in raised}
            if "ambiguous" in statuses:
                cells[cell] = "ambiguous"
            elif "found" in statuses:
                cells[cell] = "human_flagged"
            elif raised:
                cells[cell] = "disputed"
            elif verdict == "valid":
                cells[cell] = "human_missed_valid"
            else:
                cells[cell] = "clean"
                if verdict == "invalid":
                    invalid.add(cell)
    return cells, invalid


def load_adjudication(path, split):
    if path is None:
        return {}, None
    path = Path(path)
    try:
        size = path.stat().st_size
    except OSError:
        raise InputError(f"cannot read the adjudication file: {path}") from None
    if size > MAX_ADJUDICATION_BYTES:
        raise InputError(f"the adjudication file is over {MAX_ADJUDICATION_BYTES} bytes.")
    data = records.validate("review-adjudication", records.read_json(path))
    if data["split_sha256"] != split["sha256"]:
        raise InputError("the adjudication file was written for another split.")
    verdicts = {}
    for entry in data["entries"]:
        key = (entry["hunk_sha256"], entry["kind"])
        if verdicts.get(key, entry["verdict"]) != entry["verdict"]:
            raise InputError(f"the adjudication file gives two verdicts for {key[1]} on one hunk.")
        verdicts[key] = entry["verdict"]
    return verdicts, records.fingerprint(data)


# Prose rules ------------------------------------------------------------------------------


def stub_judge(data):
    """Fixed rules, no model: flag a hunk whose added lines share at least two and at least half
    of the word tokens of the rule's title."""
    rule = data["rule"]
    words = classify._tokens(rule["title"])
    flags = []
    for unit in data["units"]:
        added = " ".join(line[1:] for line in unit["hunk"].split("\n")[1:] if line.startswith("+"))
        shared = words & classify._tokens(added)
        if words and len(shared) >= 2 and 2 * len(shared) >= len(words):
            flags.append(
                {
                    "rule_id": rule["id"],
                    "unit_id": unit["id"],
                    "rationale": "Stub: shares " + ", ".join(sorted(shared)) + ".",
                }
            )
    return {"flags": flags}


def _judge_batches(units):
    batch, size = [], 0
    for unit in units:
        item = {"id": unit["id"], "pr": unit["pr"], "path": unit["path"], "hunk": unit["hunk"]}
        cost = len(records.canonical(item).encode("utf-8"))
        if cost > MAX_JUDGE_BATCH_BYTES:
            raise QualityGateError(
                f"hunk {unit['id']} is {cost} bytes, over the judge batch cap; stopped instead of "
                "cutting it."
            )
        if batch and size + cost > MAX_JUDGE_BATCH_BYTES:
            yield batch
            batch, size = [], 0
        batch.append(item)
        size += cost
    if batch:
        yield batch


def judge(session, rules, units):
    """({rule id: [unit ids]}, {rule id: reason}, rejected flags) for prose rules."""
    flagged, failures, rejected = {}, {}, []
    instructions = judge_instructions()
    for rule in rules:
        hits = set()
        try:
            for batch in _judge_batches(units):
                data = {
                    "rule": {"id": rule["id"], "title": rule["title"], "body": rule["body"]},
                    "units": batch,
                }
                output = session.ask(STAGE, instructions, data, JUDGE_SCHEMA, stub_judge)
                sent = {item["id"] for item in batch}
                for flag in output["flags"]:
                    if flag["rule_id"] != rule["id"] or flag["unit_id"] not in sent:
                        rejected.append({**flag, "call_id": session.last_call_id})
                    else:
                        hits.add(flag["unit_id"])
        except BoundaryError:
            raise
        except QualityGateError as error:
            failures[rule["id"]] = f"judge_failed: {str(error)[:200]}"
            continue
        flagged[rule["id"]] = sorted(hits)
    return flagged, failures, rejected


# Effort and cost --------------------------------------------------------------------------


def effort(run_dir, seconds):
    path = paths.child(run_dir, TIMINGS)
    stages = {}
    for row in records.read_jsonl(path) if path.is_file() else []:
        entry = stages.setdefault(row["stage"], {"runs": 0, "seconds": 0.0})
        entry["runs"] += 1
        entry["seconds"] = round(entry["seconds"] + row["seconds"], 3)
    stages["evaluate (this run, until written)"] = {"runs": 1, "seconds": round(seconds, 3)}
    return {"wall_clock": stages, "source": f"{TIMINGS} (written by the review command line)"}


def cost(run_dir):
    calls = llm.calls(run_dir)
    stages, bases = {}, set()
    for call in calls:
        entry = stages.setdefault(
            call["stage"], {"calls": 0, "charged_usd": 0.0, "unknown_cost_calls": 0}
        )
        entry["calls"] += 1
        entry["charged_usd"] = round(entry["charged_usd"] + call["charged_usd"], 6)
        entry["unknown_cost_calls"] += not call["cost_known"]
        for usage in (call.get("model_usage") or {}).values():
            if usage.get("cost_basis"):
                bases.add(usage["cost_basis"])
    return {
        "calls": len(calls),
        "charged_usd": round(sum(call["charged_usd"] for call in calls), 6),
        "by_stage": stages,
        "backends": sorted({call["backend"] for call in calls}),
        "cost_basis": sorted(bases),
        "note": "subscription costs are the CLI's conversion, not a bill; see cost_basis.",
    }


# Stage ------------------------------------------------------------------------------------


def _per_pr(cells, flagged, invalid, units, prs):
    by_pr = {number: {} for number in prs}
    pr_of = {unit["id"]: unit["pr"] for unit in units}
    for cell, label in cells.items():
        by_pr[pr_of[cell[0]]][cell] = label
    return [metrics.count(by_pr[number], flagged, invalid) for number in prs]


def _config(rule_ids, kinds, cells, findings, invalid, units, prs, thresholds):
    chosen = {cell: label for cell, label in cells.items() if cell[1] in kinds}
    flagged = {(unit, rule) for rule in rule_ids for unit in findings.get(rule, [])}
    summary = metrics.summarize(_per_pr(chosen, flagged, invalid, units, prs), len(prs), thresholds)
    return {"rules": sorted(rule_ids), **summary}


def _candidate_rules(run_dir, now):
    """Candidates to evaluate as rules, after bringing edited artifacts to a new version.

    The rule (title, form, check) comes from body.md through `candidates.content`; a record that
    disagrees with its artifact stops the stage (exit 3) before anything is written.
    """
    rules, failed, records_by_id = [], {}, {}
    for candidate_id in candidates.ids(run_dir):
        record, text = candidates.load(run_dir, candidate_id)
        record, problems = candidates.refresh(record, text, now)
        if record["status"] not in {"proposed", "evaluated"}:
            continue  # decided candidates are evaluated through the registry instead
        records_by_id[candidate_id] = (record, text)
        if problems:
            failed[candidate_id] = "invalid_artifact: " + "; ".join(problems)
            continue
        rules.append({"id": candidate_id, **candidates.content(record, text), "body": text})
    return rules, failed, records_by_id


def evaluate(run_dir, root, split, session, adjudication_path=None, thresholds=None):
    started = timer()
    thresholds = metrics.THRESHOLDS if thresholds is None else thresholds
    run_id, now = run_dir.name, records.iso()
    collect = records.read_json(paths.child(run_dir, "collect.json"))
    if split["repo"] != collect["repo"]:
        raise InputError(f"the split is for {split['repo']}, not for this run's {collect['repo']}.")
    check_holdout(run_dir, split)
    verdicts, adjudication_sha = load_adjudication(adjudication_path, split)
    units, threads, unplaced = build_units(run_dir, split)
    baseline = registry.active_rules(root, collect["repo"])
    proposed, failures, records_by_id = _candidate_rules(run_dir, now)
    rules = baseline + proposed

    declarative = [rule for rule in rules if rule["form"] == "declarative_check"]
    results, check_failures = apply_rules.apply(declarative, units)
    findings = {rule_id: result["units"] for rule_id, result in results.items()}
    prose = [rule for rule in rules if rule["form"] == "prose"]
    judged, judge_failures, rejected_flags = judge(session, prose, units) if prose else ({}, {}, [])
    findings.update(judged)
    failures.update(check_failures)
    failures.update(judge_failures)

    working = [rule for rule in rules if rule["id"] in findings]
    base_ids = [rule["id"] for rule in baseline if rule["id"] in findings]
    candidate_ids = [rule["id"] for rule in proposed if rule["id"] in findings]
    cells, invalid = label_cells(units, threads, working, verdicts)
    prs = sorted(split["evaluation"]["prs"])
    universe = set(base_ids + candidate_ids)
    configs = {
        "baseline": _config(base_ids, universe, cells, findings, invalid, units, prs, thresholds),
        "with_candidates": _config(
            base_ids + candidate_ids, universe, cells, findings, invalid, units, prs, thresholds
        ),
    }
    status, reason = "evaluated", None
    if len(prs) < thresholds["min_prs"]:
        status = "unevaluable"
        reason = f"insufficient evaluation PRs: {len(prs)} < {thresholds['min_prs']}"
    elif not universe:
        status, reason = "unevaluable", "no rule could be applied (no candidate, no baseline)"
    criteria = {
        "metrics": metrics.METRICS,
        "thresholds": thresholds,
        "labels": metrics.LABEL_RULES,
    }
    criteria_hash = metrics.criteria_hash(
        split["sha256"], metrics.METRICS, thresholds, metrics.LABEL_RULES
    )

    entries = []
    for candidate_id, (record, _) in sorted(records_by_id.items()):
        entry = {
            "id": candidate_id,
            "content_sha256": record["artifact"]["content_sha256"],
            "version": record["version"],
            "form": record["form"],
            "status": "check_failed" if candidate_id in failures else "evaluated",
            "findings": len(findings.get(candidate_id, [])),
            "config": None,
        }
        if candidate_id in failures:
            entry["reason"] = failures[candidate_id]
        else:
            kinds = set(base_ids) | {candidate_id}
            entry["config"] = _config(
                [*base_ids, candidate_id], kinds, cells, findings, invalid, units, prs, thresholds
            )
        entries.append(entry)

    unit_by_id = {unit["id"]: unit for unit in units}
    labels = dict.fromkeys(metrics.LABELS, 0)
    for label in cells.values():
        labels[label] += 1
    evaluation = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "status": status,
        "unevaluable_reason": reason,
        "criteria_hash": criteria_hash,
        "criteria": criteria,
        "split_sha256": split["sha256"],
        "adjudication_sha256": adjudication_sha,
        "n_prs": len(prs),
        "n_units": len(units),
        "labels": labels,
        "candidates": entries,
        "configs": configs,
        "baseline_rules": [
            {"id": rule["id"], "content_sha256": rule["content_sha256"]} for rule in baseline
        ],
        "failures": [{"rule_id": key, "reason": value} for key, value in sorted(failures.items())],
        "units": [
            {key: unit[key] for key in ("id", "pr", "path", "hunk_sha256", "source", "thread_ids")}
            for unit in units
        ],
        "threads_without_hunk": unplaced,
        "findings": [
            {
                "unit_id": unit_id,
                "hunk_sha256": unit_by_id[unit_id]["hunk_sha256"],
                "pr": unit_by_id[unit_id]["pr"],
                "path": unit_by_id[unit_id]["path"],
                "rule_id": rule_id,
                "label": cells[(unit_id, rule_id)],
            }
            for rule_id in sorted(findings)
            for unit_id in findings[rule_id]
        ],
        "rejected_flags": rejected_flags,
        "effort": {},
        "cost": cost(run_dir),
        "evidence": [
            {
                "id": candidate_id,
                "ref": f"runs/{run_id}/candidates/{candidate_id}/{candidates.BODY}",
                "note": f"content_sha256 {record['artifact']['content_sha256']}",
            }
            for candidate_id, (record, _) in sorted(records_by_id.items())
        ],
    }
    evaluation["effort"] = effort(run_dir, timer() - started)
    records.validate("review-evaluation", evaluation)
    records.write_json(paths.child(run_dir, "evaluation.json"), evaluation)
    paths.write_private(paths.child(run_dir, "evaluation.md"), report(evaluation, records_by_id))

    for candidate_id, (record, _) in records_by_id.items():
        if candidate_id in failures:
            pass
        elif status == "evaluated":
            record["evaluation_ref"] = "evaluation.json"
            record["criteria_hash"] = criteria_hash
            candidates.transition(record, "evaluated", "evaluate", now)
        else:
            record["evaluation_ref"] = record["criteria_hash"] = None
            candidates.transition(record, "proposed", "evaluate", now, f"unevaluable: {reason}")
        candidates.save(run_dir, record)
    return evaluation


# Output -----------------------------------------------------------------------------------


def _shown(metric):
    if not metric["defined"]:
        return metric.get("reason", "undefined").split(":")[0]
    interval = metric["ci95"]
    return f"{metric['value']:.3f}" + (
        f" [{interval[0]:.3f}, {interval[1]:.3f}]" if interval else ""
    )


def report(evaluation, records_by_id):
    """evaluation.md. Titles and paths are escaped; nothing external is left as Markdown."""
    lines = [
        f"# Review evaluation {evaluation['run_id']}",
        "",
        f"- status: {evaluation['status']}"
        + (
            f" ({escape(evaluation['unevaluable_reason'])})"
            if evaluation["unevaluable_reason"]
            else ""
        ),
        f"- criteria_hash: `{evaluation['criteria_hash']}`",
        f"- split: `{evaluation['split_sha256']}`; evaluation PRs (n): {evaluation['n_prs']}; "
        f"hunks: {evaluation['n_units']}",
        "- precision_lower counts every finding no person confirmed as wrong; "
        "precision_confirmed uses only findings a reviewer raised or a person adjudicated.",
        "- Intervals are 95% percentile intervals from resampling evaluation PRs "
        f"(seed {evaluation['criteria']['thresholds']['seed']}, "
        f"{evaluation['criteria']['thresholds']['resamples']} resamples).",
        "- Numbers from synthetic data check the mechanism; they are not a claim of accuracy.",
        "",
        "## Labels (hunk x kind)",
        "",
        "| label | cells |",
        "|---|---|",
    ]
    lines += [f"| {name} | {value} |" for name, value in evaluation["labels"].items()]
    lines += [
        "",
        "## Configurations",
        "",
        "| configuration | rules | " + " | ".join(metrics.METRICS) + " |",
        "|---|---|" + "---|" * len(metrics.METRICS),
    ]
    rows = [(name, config) for name, config in evaluation["configs"].items()]
    rows += [
        (f"baseline + {entry['id']}", entry["config"])
        for entry in evaluation["candidates"]
        if entry["config"] is not None
    ]
    for name, config in rows:
        shown = " | ".join(_shown(config["metrics"][metric]) for metric in metrics.METRICS)
        lines.append(f"| {name} | {len(config['rules'])} | {shown} |")
    lines += ["", "## Candidates", "", "| id | version | form | status | findings | lint | title |"]
    lines.append("|---|---|---|---|---|---|---|")
    for entry in evaluation["candidates"]:
        record, _ = records_by_id[entry["id"]]
        lines.append(
            f"| {entry['id']} | {entry['version']} | {entry['form']} | {entry['status']} | "
            f"{entry['findings']} | {len(record['lint'])} | {escape(record['title'], 120)} |"
        )
    if evaluation["failures"]:
        lines += ["", "## Rules that could not be applied", ""]
        lines += [
            f"- {item['rule_id']}: {escape(item['reason'], 300)}" for item in evaluation["failures"]
        ]
    lines += ["", "## Findings", "", "| rule | PR | path | label |", "|---|---|---|---|"]
    lines += [
        f"| {item['rule_id']} | {item['pr']} | {escape(item['path'], 200)} | {item['label']} |"
        for item in evaluation["findings"]
    ]
    lines += [
        "",
        "## Cost and effort",
        "",
        code_block(records.canonical(evaluation["cost"]), "json"),
    ]
    lines += ["", code_block(records.canonical(evaluation["effort"]), "json"), ""]
    return "\n".join(lines)


def evaluator_output(evaluation):
    """The improvement harness's evaluator output (`evaluation.schema.json#/$defs/output`)."""
    output = {
        "metrics": {},
        "evidence": [
            {
                "id": "evaluation",
                "ref": f"runs/{evaluation['run_id']}/evaluation.json",
                "note": f"criteria_hash {evaluation['criteria_hash']}",
            },
            *evaluation["evidence"],
        ],
        "notes": (
            f"sre-harness review evaluate; n={evaluation['n_prs']} evaluation PRs, "
            f"{evaluation['n_units']} hunks; metrics without a prefix are baseline + candidates."
        ),
    }
    if evaluation["status"] != "evaluated":
        output["unevaluable"] = evaluation["unevaluable_reason"]
        return output
    undefined = []
    thresholds = evaluation["criteria"]["thresholds"]
    for prefix, config in (
        ("", evaluation["configs"]["with_candidates"]),
        ("baseline_", evaluation["configs"]["baseline"]),
    ):
        for name, metric in config["metrics"].items():
            if not metric["defined"]:
                undefined.append(prefix + name)
                continue
            output["metrics"][prefix + name] = {
                "value": metric["value"],
                "rationale": (
                    f"{metrics.METRICS[name]}; n={evaluation['n_prs']} PRs; 95% CI "
                    f"{metric['ci95']} by PR bootstrap (seed {thresholds['seed']}, "
                    f"{thresholds['resamples']} resamples)"
                ),
            }
    if undefined:
        output["notes"] += " Undefined (denominator 0): " + ", ".join(undefined) + "."
    return output


def summary(evaluation):
    return {
        "run_id": evaluation["run_id"],
        "status": evaluation["status"],
        "unevaluable_reason": evaluation["unevaluable_reason"],
        "criteria_hash": evaluation["criteria_hash"],
        "n_prs": evaluation["n_prs"],
        "n_units": evaluation["n_units"],
        "labels": evaluation["labels"],
        "candidates": [
            {key: entry[key] for key in ("id", "status", "findings")}
            for entry in evaluation["candidates"]
        ],
        "with_candidates": evaluation["configs"]["with_candidates"]["counts"],
    }
