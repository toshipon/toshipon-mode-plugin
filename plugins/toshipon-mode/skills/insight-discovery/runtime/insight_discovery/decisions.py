"""What only a person decides: their own hypothesis first, which experiment to run, what is searched.

Every approval goes through the foundation's human gate, which refuses under a scheduled or
harness-started process. Nothing here runs an experiment or sends anything anywhere.
"""

import difflib
import os
from urllib.parse import urlsplit

from . import ranking, report, routing
from . import state as st
from .evidence import deny_terms, scrub
from .foundation import (
    ACTIVE_ENV,
    AUTOMATED_ENV,
    SCHEMAS,
    HarnessError,
    Refusal,
    find_deny_terms,
    find_secrets,
    iso,
    require_human,
    validate,
    write_private,
)

TRANSITIONS = {"candidate": ("rejected",), "accepted": ("rejected", "tested")}
QUOTE_LENGTH = 12  # a query sharing this many consecutive characters with evidence quotes it


def _approval(reviewer, reason):
    try:
        return require_human(reviewer, reason)
    except HarnessError as refusal:
        raise Refusal(str(refusal)) from None


def _by_a_person(env=None):
    """Records that speak for a person are refused from a scheduled or harness-started process."""
    env = os.environ if env is None else env
    if env.get(AUTOMATED_ENV) or env.get(ACTIVE_ENV):
        raise Refusal("this is recorded by a person; it cannot be done from an automated run")


def _candidate(state, insight_id):
    if insight_id not in state["candidates"]:
        raise HarnessError(f"unknown candidate: {insight_id}")
    return state["candidates"][insight_id]


def _unanswered_week(home, now):
    """This week, if the person has neither written nor skipped their own hypothesis yet.

    Once either is on record the generated hypotheses are visible, so a later "prior" would
    no longer be prior to anything.
    """
    state, week = st.load(home), st.week_of(now)
    if week in state["priors"] or week in state["prior_skips"]:
        raise Refusal(f"the prior for {week} is already recorded or skipped; it is written once")
    return week


def prior(home, config, text, now):
    """Record the person's own hypothesis for this week, before they are shown the generated ones."""
    _by_a_person()
    if not (text or "").strip():
        raise HarnessError("the prior needs text")
    cleaned, _ = scrub(text.strip(), deny_terms(config))
    week = _unanswered_week(home, now)
    st.append(home, "prior_recorded", {"week": week, "text": cleaned}, now)
    return {"week": week, "recorded": True}


def skip_prior(home, reason, now):
    _by_a_person()
    if not (reason or "").strip():
        raise HarnessError("skipping the prior needs a reason")
    week = _unanswered_week(home, now)
    st.append(home, "prior_skipped", {"week": week, "reason": reason.strip()}, now)
    return {"week": week, "skipped": True}


def weekly(home, settings, now):
    """Write this week's report and mark the candidates in it as waiting for a choice."""
    state = st.load(home)
    path = st.root(home) / "reports" / f"weekly-{now:%Y-%m-%d}.md"
    write_private(path, report.render(state, settings, now))
    shown = [candidate["id"] for candidate in ranking.presentable(state, settings)]
    data = {"week": st.week_of(now), "report": str(path), "presented": shown}
    st.append(home, "weekly_reported", data, now)
    return {"action": "reported", **data}


def select(home, settings, insight_id, reviewer, reason, now):
    """A person accepts at most one candidate per week as the experiment to run."""
    approval = _approval(reviewer, reason)
    state = st.load(home)
    candidate, week = _candidate(state, insight_id), st.week_of(now)
    if week in state["selections"]:
        raise Refusal(f"{state['selections'][week]} was already selected in {week}; one per week")
    if candidate["id"] not in {item["id"] for item in ranking.presentable(state, settings)}:
        raise Refusal(f"{insight_id} is not among the presented research candidates")
    data = {"insight_id": insight_id, "approval": approval, "week": week}
    st.append(home, "selected", data, now)
    return {"insight_id": insight_id, "status": "accepted", "week": week}


def resolve(home, insight_id, status, reviewer, reason, now):
    """A person moves candidate -> rejected, or accepted -> rejected | tested. Both ends are final."""
    approval = _approval(reviewer, reason)
    reason, actor = approval["reason"], approval["reviewer"]
    candidate = _candidate(st.load(home), insight_id)
    if status not in TRANSITIONS.get(candidate["status"], ()):
        raise Refusal(f"{insight_id} is {candidate['status']}; it cannot become {status}")
    data = {"insight_id": insight_id, "status": status, "reason": reason, "actor": actor}
    st.append(home, "resolved", data, now)
    return {"insight_id": insight_id, "status": status}


def handoff(home, settings, insight_id, target, reviewer, reason, now):
    """A person hands an open research candidate to a neighbouring skill. It leaves the list."""
    reason = _approval(reviewer, reason)["reason"]
    state = st.load(home)
    candidate = _candidate(state, insight_id)
    if candidate["status"] != "candidate" or candidate["routing"]["disposition"] != "research":
        raise Refusal(f"{insight_id} is not an open research candidate")
    note = routing.handoff(home, candidate, target, reason, now)
    return {"insight_id": insight_id, "target": target, "handoff": note["handoff_ref"]}


def abandon(home, reviewer, reason, now):
    """A person closes an open cycle that cannot finish. Its evidence counts as looked at."""
    approval = _approval(reviewer, reason)
    state = st.load(home)
    if not state["open_cycle"]:
        raise HarnessError("no analysis cycle is open")
    data = {
        "cycle_id": state["open_cycle"],
        "result": "abandoned",
        "presented": [],
        "handoffs": [],
        "skipped": [],
        "approval": approval,
    }
    st.append(home, "cycle_finished", data, now)
    return {"cycle_id": data["cycle_id"], "result": "abandoned"}


def _quotes_evidence(query, texts):
    lowered = query.lower()
    for text in texts:
        match = difflib.SequenceMatcher(None, lowered, text.lower(), autojunk=False)
        if match.find_longest_match().size >= QUOTE_LENGTH:
            return True
    return False


def search_approve(home, config, settings, insight_id, queries, reviewer, reason, now):
    """Check and approve the queries of one search. The search itself is run by the person's session."""
    approval = _approval(reviewer, reason)
    state = st.load(home)
    candidate, limits = _candidate(state, insight_id), settings["search"]
    if candidate["status"] in ("rejected", "tested"):
        raise Refusal(f"{insight_id} is {candidate['status']}; it is no longer changed")
    queries = [query.strip() for query in queries if query.strip()]
    if not queries or len(queries) > limits["max_queries"]:
        raise Refusal(f"a search takes 1 to {limits['max_queries']} queries")
    terms = deny_terms(config)
    texts = [item["text"] for item in state["evidence"].values() if "text" in item]
    names = {item["context"]["task_key"].lower() for item in state["evidence"].values()}
    for query in queries:
        if len(query) > limits["max_query_chars"]:
            problem = "is longer than search.max_query_chars"
        elif find_secrets(query):
            problem = "contains a secret-shaped string"
        elif find_deny_terms(query, terms):
            problem = "contains a forbidden term"
        elif any(name in query.lower() for name in names):
            problem = "names a task"
        elif _quotes_evidence(query, texts):
            problem = "quotes recorded evidence; use abstract terms"
        else:
            continue
        raise Refusal(f"query not sent: it {problem}")  # the query itself is not echoed
    data = {"insight_id": insight_id, "queries": queries, "approval": approval}
    st.append(home, "search_approved", data, now)
    return {"insight_id": insight_id, "approved_queries": queries}


def search_record(home, config, settings, insight_id, results, now):
    """Record what an approved search found. Novelty becomes a status derived from it."""
    _by_a_person()
    validate(f"{SCHEMAS}:stages#search_results", results)
    state = st.load(home)
    candidate = _candidate(state, insight_id)
    if candidate["status"] in ("rejected", "tested"):
        raise Refusal(f"{insight_id} is {candidate['status']}; it is no longer changed")
    approved = [sorted(item["queries"]) for item in state["searches"].get(insight_id, [])]
    if sorted(results["queries"]) not in approved:
        raise Refusal("these queries were not approved; run `search approve` first")
    allowed, terms, prior_art = settings["search"]["allowed_domains"], deny_terms(config), []
    for item in results["prior_art"]:
        url = urlsplit(item["url"])
        host = (url.hostname or "").lower()
        if url.scheme != "https" or not host or url.username or url.password:
            raise Refusal("a source needs an https URL without credentials")
        if not any(host == name or host.endswith(f".{name}") for name in allowed):
            raise Refusal(
                f"source host is not in search.allowed_domains: {host} "
                "(list the public sources you allow; the default is empty)"
            )
        if (
            find_deny_terms(item["url"], terms)
            or find_secrets(item["url"])
            or set("()<>[]") & set(item["url"])
        ):
            raise Refusal("a source URL contains a forbidden term, a secret shape or brackets")
        prior_art.append(
            {
                "title": scrub(item["title"], terms)[0],
                "url": item["url"],
                "retrieved_at": iso(now),
                "claim": scrub(item["claim"], terms)[0],
                "relation": item["relation"],
            }
        )
    relations = {item["relation"] for item in prior_art}
    if "same" in relations:
        status = "covered_by_existing"
    elif "partial" in relations:
        status = "partial_overlap"
    else:
        status = "searched_none_found"
    novelty = {"status": status, "queries": sorted(results["queries"]), "searched_at": iso(now)}
    data = {"insight_id": insight_id, "prior_art": prior_art, "novelty": novelty}
    st.append(home, "prior_art_recorded", data, now)
    return {"insight_id": insight_id, "novelty": novelty, "prior_art": len(prior_art)}
