"""The discovery loop: which stage comes next, what a model is shown, and what is kept.

`next_action` is a decision table over the ledger. A model fills in one stage at a time;
checks.py decides what survives, routing.py who it belongs to, ranking.py what is presented.
"""

import time

from . import checks, evidence, ranking, routing, signals
from . import state as st
from .foundation import (
    STAGE_ROLES,
    GateFailed,
    HarnessError,
    Refusal,
    agents,
    iso,
    locked,
    sha256,
    validate,
)

# What the auditor is told the neighbours already do, so "an existing method is enough" has a referent.
EXISTING_CAPABILITIES = [
    {
        "name": "autonomous-improvement",
        "handles": "成果物（コード、設計書、IaC、運用手順）を、測定できる合格条件に向けて反復改善する",
        "recommendation": "handoff_improvement",
    },
    {
        "name": "harness-evolution",
        "handles": "繰り返す失敗から Skill・指示・ツール・評価の改善候補を作り、現行版と比較する",
        "recommendation": "handoff_evolution",
    },
]
NOT_SEARCHED = "既存手法との重なりは未調査（外部検索をしていない）"


def _open(state):
    if not state["open_cycle"]:
        raise GateFailed("no analysis cycle is open; `next` says what to do")
    return state["cycles"][state["open_cycle"]]


def _carries(stage, recorded):
    """Whether the stage left anything for the next one to work on."""
    accepted = recorded["accepted"]
    if stage == "audit":
        return any(route["disposition"] == "research" for route in accepted["routes"].values())
    return bool(
        accepted.get({"observe": "observations", "structure": "groups"}.get(stage, "hypotheses"))
    )


def pending_stage(cycle):
    """The first stage without a record, or None when the cycle only needs closing."""
    for stage in st.STAGES:
        recorded = cycle["stages"].get(stage)
        if not recorded:
            return stage
        if stage == "assess" or not _carries(stage, recorded):
            return None
    return None


def next_action(state, allowed=None):
    """What to do next, from the ledger alone. `allowed` is the llm_policy's classifications."""
    if state["open_cycle"]:
        cycle = state["cycles"][state["open_cycle"]]
        stage = pending_stage(cycle)
        if stage:
            return {"action": "stage", "stage": stage, "cycle_id": cycle["cycle_id"]}
        return {"action": "finalize", "cycle_id": cycle["cycle_id"]}
    fresh = st.fresh(state, allowed)
    if fresh:
        return {"action": "start_cycle", "fresh": len(fresh)}
    step: dict = {"action": "none", "reason": "no new evidence since the last cycle"}
    if allowed is not None and st.withheld(state, allowed):
        # Evidence the policy does not let a model see never enters a cycle; it is only counted.
        step["withheld_by_llm_policy"] = st.withheld(state, allowed)
    return step


def start_cycle(home, settings, now, allowed=None):
    """Open a cycle over the new evidence, topped up with earlier evidence for cross-task links."""
    state = st.load(home)
    fresh, limit = st.fresh(state, allowed), settings["limits"]["max_evidence_per_cycle"]
    if state["open_cycle"] or not fresh:
        raise HarnessError("a cycle starts only when none is open and there is new evidence")
    chosen = fresh[:limit]
    earlier = [
        name
        for name, item in reversed(list(state["evidence"].items()))
        if name not in set(fresh) and (allowed is None or item["classification"] in allowed)
    ]
    data = {
        "cycle_id": f"C-{now:%Y%m%d}-{len(state['cycles']) + 1:02d}",
        "evidence_ids": chosen + earlier[: limit - len(chosen)],
        "fresh": chosen,
        "deferred": len(fresh) - len(chosen),
    }
    st.append(home, "cycle_started", data, now)
    return data


def _brief(item):
    brief = {
        "id": item["id"],
        "kind": item["kind"],
        "label": item["label"],
        "task_key": item["context"]["task_key"],
        "domain_tag": item["context"]["domain_tag"],
        "occurrences": item["counts"]["occurrences"],
    }
    if "text" in item:
        brief["text"] = item["text"]
    return brief


def _cited(observations, group):
    """The group's observations with their ids, so a text that says "O4" can be followed."""
    return [{"id": name, "text": observations[name]["text"]} for name in group["observations"]]


def _hypothesis_views(cycle, only=None):
    done = cycle["stages"]
    groups = {group["id"]: group for group in done["structure"]["accepted"]["groups"]}
    observations = {item["id"]: item for item in done["observe"]["accepted"]["observations"]}
    views = []
    for hypothesis in done["hypothesize"]["accepted"]["hypotheses"]:
        if only is not None and hypothesis["id"] not in only:
            continue
        group = groups[hypothesis["group"]]
        views.append(
            {
                **{key: value for key, value in hypothesis.items() if key != "group"},
                "abstracted_problem": group["abstracted_problem"],
                "levels": group["levels"],
                "doubted_premise": group["doubted_premise"],
                "evidence_summary": group["evidence_summary"],
                "observations": _cited(observations, group),
            }
        )
    return views


def research_ids(cycle):
    routes = cycle["stages"]["audit"]["accepted"]["routes"]
    return [name for name, route in routes.items() if route["disposition"] == "research"]


def payload(state, cycle, stage):
    """(what the model is shown, its data classification). All of it is data, none of it instruction."""
    items = [state["evidence"][name] for name in cycle["evidence_ids"]]
    done = cycle["stages"]
    if stage == "observe":
        body = {"evidence": [_brief(item) for item in items], "signals": signals.extract(items)}
    elif stage == "structure":
        accepted = done["observe"]["accepted"]
        tasks = {item["id"]: item["context"]["task_key"] for item in items}
        body = {
            "observations": [
                {**item, "task_keys": sorted({tasks[ref] for ref in item["evidence_refs"]})}
                for item in accepted["observations"]
            ],
            "interpretations": accepted["interpretations"],
        }
    elif stage == "hypothesize":
        statements = {"rejected": [], "open": []}
        for candidate in state["candidates"].values():
            bucket = "rejected" if candidate["status"] == "rejected" else "open"
            statements[bucket].append(candidate["hypothesis"]["statement"])
        observations = {item["id"]: item for item in done["observe"]["accepted"]["observations"]}
        body = {
            "groups": [
                {**group, "observations": _cited(observations, group)}
                for group in done["structure"]["accepted"]["groups"]
            ],
            "already_rejected": statements["rejected"],
            "already_open": statements["open"],
        }
    elif stage == "audit":
        body = {
            "hypotheses": _hypothesis_views(cycle),
            "existing_capabilities": EXISTING_CAPABILITIES,
        }
    else:
        audits = {item["hypothesis"]: item for item in done["audit"]["accepted"]["audits"]}
        body = {
            "hypotheses": [
                {**view, "audit_findings": audits[view["id"]]["findings"]}
                for view in _hypothesis_views(cycle, only=research_ids(cycle))
            ],
            "novelty": "not_searched",
        }
    classification = agents.strictest(*(item["classification"] for item in items))
    return {"stage": stage, **body}, classification


def assert_policy(config, classification):
    allowed = config["llm_policy"]["allowed_classifications"]
    if classification not in allowed:
        raise Refusal(
            f"evidence classified {classification} may not be shown to a model: the foundation's "
            f"llm_policy.allowed_classifications lists only {allowed}"
        )


def record_stage(home, stage, output, now, settings, independence="same_context"):
    """Check one stage's output and append what survives. Returns the appended data."""
    validate(STAGE_ROLES[stage][1], output)
    if independence not in (*st.INDEPENDENT, "same_context"):
        raise HarnessError("independence must be separate_process, subagent or same_context")
    state = st.load(home)
    cycle = _open(state)
    if pending_stage(cycle) != stage:
        raise GateFailed(f"stage {stage} is not the next one; `next` says what is")
    done, gates = cycle["stages"], settings["gates"]
    if stage == "observe":
        known = {name: state["evidence"][name] for name in cycle["evidence_ids"]}
        accepted, rejected = checks.observe(output, known)
    elif stage == "structure":
        observations = done["observe"]["accepted"]["observations"]
        accepted, rejected = checks.structure(output, observations, state["evidence"], gates)
    elif stage == "hypothesize":
        groups = done["structure"]["accepted"]["groups"]
        accepted, rejected = checks.hypothesize(output, groups, state, gates)
    elif stage == "audit":
        audits, rejected = checks.audit(output, done["hypothesize"]["accepted"]["hypotheses"])
        routes = {}
        for item in audits:
            disposition, reason = routing.route(item, independence)
            routes[item["hypothesis"]] = {"disposition": disposition, "reason": reason}
        accepted = {
            "audits": audits,
            "routes": routes,
            "independence": independence,
            "limitations": output["limitations"],
        }
    else:
        accepted, rejected = checks.assess(output, research_ids(cycle))
    data = {
        "cycle_id": cycle["cycle_id"],
        "stage": stage,
        "accepted": accepted,
        "rejected": rejected,
    }
    st.append(home, "stage_recorded", data, now)
    return data


def _candidate(cycle, hypothesis, route, now):
    """The candidate object for one hypothesis, without an id."""
    done = cycle["stages"]
    group = next(
        g for g in done["structure"]["accepted"]["groups"] if g["id"] == hypothesis["group"]
    )
    observed = done["observe"]["accepted"]
    observations = [
        item for item in observed["observations"] if item["id"] in group["observations"]
    ]
    position = {item["id"]: index for index, item in enumerate(observations)}
    audit_stage = done["audit"]["accepted"]
    audit = next(a for a in audit_stage["audits"] if a["hypothesis"] == hypothesis["id"])
    candidate = {
        "schema_version": 1,
        "run_id": cycle["cycle_id"],
        "created_at": iso(now),
        "fingerprint": sha256("".join(hypothesis["statement"].split()).lower()),
        "observations": [
            {key: item[key] for key in ("text", "evidence_refs", "quotes")} for item in observations
        ],
        "interpretations": [
            {"text": item["text"], "based_on": [position[name] for name in item["based_on"]]}
            for item in observed["interpretations"]
            if all(name in position for name in item["based_on"])
        ],
        "evidence_refs": group["evidence_refs"],
        "evidence_summary": group["evidence_summary"],
        "abstracted_problem": group["abstracted_problem"],
        "levels": group["levels"],
        "doubted_premise": group["doubted_premise"],
        "hypothesis": {"statement": hypothesis["statement"], "generator": hypothesis["generator"]},
        "analogy": hypothesis["analogy"],
        "prior_art": [],
        "novelty": {"status": "not_searched", "queries": [], "searched_at": None},
        "refutation": {
            "falsification": hypothesis["falsification"],
            "counter_evidence": hypothesis["counter_evidence"],
            "audit": {
                "auditor": {"name": "insight-auditor", "independence": audit_stage["independence"]},
                **{key: value for key, value in audit.items() if key != "hypothesis"},
            },
        },
        "unverified": [NOT_SEARCHED],
        "status": "candidate",
        "history": [
            {
                "from": None,
                "to": "candidate",
                "at": iso(now),
                "actor": "insight-discovery",
                "reason": f"cycle {cycle['cycle_id']}",
            }
        ],
        "human_prior": None,
        "routing": route,
    }
    assessed = done.get("assess", {}).get("accepted", {}).get("assessments", [])
    assessment = next((a for a in assessed if a["hypothesis"] == hypothesis["id"]), None)
    if route["disposition"] == "research" and assessment:
        candidate.update(
            {
                key: assessment[key]
                for key in ("minimal_experiment", "success_criteria", "kill_criteria")
            }
        )
        candidate.update(
            estimated_cost=assessment["estimated_cost"],
            confidence=assessment["confidence"],
            confidence_reason=assessment["confidence_reason"],
            unverified=[*assessment["unverified"], NOT_SEARCHED],
            scores={
                **assessment["scores"],
                # No search is on record, so a difference from prior art cannot raise the rank.
                "delta_from_prior_art": {
                    "score": 1,
                    "reason": "未調査のため 1 に固定。モデルの見立て: "
                    + assessment["scores"]["delta_from_prior_art"]["reason"],
                },
                "evidence": ranking.evidence_score(group["evidence_summary"]),
            },
        )
    return candidate


def finalize(home, now, settings):
    """Close the open cycle: register what is presented or handed off, and list what is not."""
    state = st.load(home)
    cycle = _open(state)
    done = cycle["stages"]
    skipped = [
        {"stage": stage, **item}
        for stage in st.STAGES
        if stage in done
        for item in done[stage]["rejected"]
    ]
    already = {
        candidate["fingerprint"]: candidate["id"]
        for candidate in state["candidates"].values()
        if candidate["run_id"] == cycle["cycle_id"]
    }
    taken = sum(1 for name in state["candidates"] if name.startswith(f"INS-{now:%Y%m%d}-"))
    noted = {note["insight_id"] for note in state["handoffs"]}
    presented, handed = [], []

    def register(candidate):
        nonlocal taken
        if candidate["fingerprint"] in already:  # a retry after a crash mid-way
            candidate["id"] = already[candidate["fingerprint"]]
            return candidate["id"]
        taken += 1
        candidate["id"] = f"INS-{now:%Y%m%d}-{taken:02d}"
        st.append(home, "candidate_registered", candidate, now)
        return candidate["id"]

    if "audit" in done:
        routes = done["audit"]["accepted"]["routes"]
        research = []
        for hypothesis in done["hypothesize"]["accepted"]["hypotheses"]:
            route = routes.get(hypothesis["id"])
            if not route:
                continue  # not audited: already listed among the rejected
            if route["disposition"] == "skip":
                skipped.append(
                    {"stage": "audit", "ref": hypothesis["id"], "reason": route["reason"]}
                )
                continue
            candidate = _candidate(cycle, hypothesis, route, now)
            if route["disposition"] == "research":
                if "scores" in candidate:
                    research.append((hypothesis["id"], candidate))
                continue
            handed.append(register(candidate))
            if candidate["id"] not in noted:
                target = routing.HANDOFFS[route["disposition"]]
                routing.handoff(home, candidate, target, route["reason"], now)
        ordered = ranking.rank([candidate for _, candidate in research])
        refs = {candidate["fingerprint"]: ref for ref, candidate in research}
        limit = min(settings["gates"]["max_candidates"], ranking.MAX_PRESENTED)
        for position, candidate in enumerate(ordered):
            if position < limit:
                presented.append(register(candidate))
            else:
                skipped.append(
                    {"stage": "rank", "ref": refs[candidate["fingerprint"]], "reason": "below_cut"}
                )
    data = {
        "cycle_id": cycle["cycle_id"],
        "result": "candidates" if presented else "none",
        "presented": presented,
        "handoffs": handed,
        "skipped": skipped,
    }
    st.append(home, "cycle_finished", data, now)
    return data


def agent_invoker(home, config, settings, budget=None):
    """Run one stage through the foundation's agent adapters (policy, budget, least privilege)."""

    def call(stage, body, classification):
        role, _, adapter = STAGE_ROLES[stage]
        if adapter not in settings["agents"]:
            raise HarnessError(
                f"no adapter configured for insight agents.{adapter}; set it in insight/config.json, "
                "or run the stage by hand with `step prompt` and `step record`"
            )
        merged = {**config, "agents": {role: settings["agents"][adapter]}}
        output, _ = agents.invoke(merged, role, body, home, classification, False, budget)
        return output

    return call


def _ingest_sources(home, config, settings, now, write):
    """Stats per configured source, in order. With write=False nothing is appended."""
    report, allowed = [], config["llm_policy"]["allowed_classifications"]
    for source in settings["sources"]:
        adapter, path = source["adapter"], source.get("path")
        try:
            if write:
                stats = evidence.ingest(home, adapter, path, config, settings, now)
            else:
                known = st.load(home)["evidence"]
                added, stats = evidence.collect(home, adapter, path, config, settings, now, known)
                stats = {**stats, "eligible": sum(i["classification"] in allowed for i in added)}
        except Refusal as refusal:
            # One refused source (an oversized file, a link) must not starve the others.
            report.append({"adapter": adapter, "refused": str(refusal), "stats": {}})
            continue
        report.append({"adapter": adapter, "stats": stats})
    return report


def plan(home, config, settings, now):
    """Dry run: what a daily run would read and do. Nothing is written and no model is called."""
    ingested = _ingest_sources(home, config, settings, now, write=False)
    state = st.load(home)
    allowed = config["llm_policy"]["allowed_classifications"]
    step = next_action(state, allowed)
    added = sum(source["stats"].get("eligible", 0) for source in ingested)
    if step["action"] == "stage":
        calls = len(st.STAGES) - st.STAGES.index(step["stage"])
    elif step["action"] == "start_cycle" or (step["action"] == "none" and added):
        step = {"action": "start_cycle", "fresh": len(st.fresh(state, allowed)) + added}
        calls = len(st.STAGES)
    else:
        calls = 0
    limit = settings["limits"]["max_llm_calls_per_run"]
    return {
        "dry_run": True,
        "ingest": ingested,
        "next": step,
        "estimated_llm_calls": min(calls, limit),
        "llm_call_limit": limit,
    }


def run_daily(home, config, settings, now, invoke=None, budget=None, timer=time.monotonic):
    """Ingest what is new, then advance the loop until it is done or a limit stops it."""
    if not invoke:
        # A run started by hand spends from the same daily cap as the scheduled ones.
        budget = budget or agents.Budget(home, now, config["budget"]["max_cost_per_day"])
        invoke = agent_invoker(home, config, settings, budget)
    limits, started, calls, output = settings["limits"], timer(), 0, None
    allowed = config["llm_policy"]["allowed_classifications"]
    with locked(st.root(home) / "run.lock", blocking=False):
        summary: dict = {
            "ingest": _ingest_sources(home, config, settings, now, write=True),
            "cycles": [],
        }
        while True:
            state = st.load(home)
            step = next_action(state, allowed)
            if step["action"] == "start_cycle":
                start_cycle(home, settings, now, allowed)
            elif step["action"] == "finalize":
                summary["cycles"].append(finalize(home, now, settings))
            elif step["action"] == "stage":
                cycle, stage = _open(state), step["stage"]
                body, classification = payload(state, cycle, stage)
                assert_policy(config, classification)
                exhausted = None
                if calls >= limits["max_llm_calls_per_run"]:
                    exhausted = "llm_calls"
                elif timer() - started >= limits["max_seconds_per_run"]:
                    exhausted = "seconds"
                else:
                    try:
                        output = invoke(stage, body, classification)
                    except agents.BudgetExceeded:
                        exhausted = "cost"
                if exhausted:
                    # Not cut short silently: the stop is on record and the next run resumes here.
                    stop = {"cycle_id": cycle["cycle_id"], "stage": stage, "limit": exhausted}
                    st.append(home, "budget_exhausted", stop, now)
                    summary["stopped"] = {"reason": "budget_exhausted", **stop}
                    break
                calls += 1
                record_stage(home, stage, output, now, settings, independence="separate_process")
            else:
                break
        summary["llm_calls"] = calls
        summary["next"] = next_action(st.load(home), allowed)
        return summary


def step_prompt(home, config, settings, stage, now):
    """What an interactive session needs to run one stage by hand. Opens a cycle when one is due."""
    state, allowed = st.load(home), config["llm_policy"]["allowed_classifications"]
    if next_action(state, allowed)["action"] == "start_cycle":
        start_cycle(home, settings, now, allowed)
        state = st.load(home)
    step = next_action(state, allowed)
    if step["action"] != "stage" or step["stage"] != stage:
        raise GateFailed(f"stage {stage} is not the next one: {step}")
    body, classification = payload(state, _open(state), stage)
    assert_policy(config, classification)
    role, schema, _ = STAGE_ROLES[stage]
    return {
        "cycle_id": step["cycle_id"],
        "stage": stage,
        "role": role,
        "agent": agents.ROLES[role]["agent"],
        "schema": schema,
        "classification": classification,
        "data": body,
    }


def step_record(home, settings, stage, output, independence, now):
    """Record a stage run by hand, then close the cycle if nothing is left to do."""
    recorded = record_stage(home, stage, output, now, settings, independence)
    result = {"stage": stage, "accepted": recorded["accepted"], "rejected": recorded["rejected"]}
    if next_action(st.load(home))["action"] == "finalize":
        result["cycle"] = finalize(home, now, settings)
    result["next"] = next_action(st.load(home))
    return result
