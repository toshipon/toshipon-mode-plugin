"""Answering from the index: find the entities a question is about, walk their relationships a
fixed number of hops, and plan which sources to fetch inside a token budget. It returns
metadata and a retrieval plan. It never returns a document body."""

import re
from collections import deque

from .base import Refusal, estimate_tokens
from .index import edges

MAX_HOPS = 4
MAX_ENTITIES = 40
MAX_TOKEN_BUDGET = 60000
DEFAULT_SOURCE_TOKENS = 600  # a source whose size was never recorded
_WORD = re.compile(r"[a-z0-9_]+")
_CJK = re.compile(r"[぀-ヿ㐀-鿿]+")
_STOP = frozenset(
    {
        "the",
        "a",
        "an",
        "is",
        "are",
        "of",
        "to",
        "in",
        "for",
        "and",
        "or",
        "why",
        "how",
        "what",
        "does",
    }
)


def terms(text):
    """Lowercase words, and character bigrams for Japanese, which has no spaces to split on."""
    text = text.lower()
    found = {word for word in _WORD.findall(text) if word not in _STOP and len(word) > 1}
    for run in _CJK.findall(text):
        found |= {run[i : i + 2] for i in range(len(run) - 1)}
    return found


def _haystack(entity):
    return " ".join(
        [entity["id"], entity["type"], entity["description"], entity.get("config_path", "")]
    )


def search(index, question, limit=3):
    """Entity ids by how many of the question's terms their metadata shares, best first."""
    wanted = terms(question)
    scored = []
    for entity in index["entities"].values():
        score = len(wanted & terms(_haystack(entity)))
        if entity["id"].lower() in question.lower():
            score += 5
        if score:
            scored.append((-score, entity["id"]))
    return [name for _, name in sorted(scored)[:limit]]


def _bounded(name, value, low, high):
    if not isinstance(value, int) or not low <= value <= high:
        raise Refusal(f"{name} must be between {low} and {high}: {value!r}")
    return value


def explore(index, *, entity=None, question="", hops=1, max_entities=12, token_budget=4000):
    """Seeds, their neighbourhood up to `hops`, and the sources to fetch within the budget.
    What did not fit is counted (`frontier`, `dropped`, `deferred`), so widening is a decision."""
    _bounded("hops", hops, 0, MAX_HOPS)
    _bounded("max_entities", max_entities, 1, MAX_ENTITIES)
    _bounded("token_budget", token_budget, 0, MAX_TOKEN_BUDGET)
    entities = index["entities"]
    if entity is not None and entity not in entities:
        raise Refusal(f"entity is not in the index: {entity}")
    seeds = [entity] if entity else search(index, question)
    neighbours = {name: [] for name in entities}
    links = edges(index)
    for start, relation, end in links:
        neighbours[start].append(end)
        neighbours[end].append(start)
    distance = {seed: 0 for seed in seeds}
    queue = deque(seeds)
    frontier = set()
    while queue:
        name = queue.popleft()
        for other in sorted(neighbours[name]):
            if other in distance:
                continue
            if distance[name] == hops:
                frontier.add(other)
                continue
            distance[other] = distance[name] + 1
            queue.append(other)
    wanted = terms(question)
    ranked = sorted(
        distance, key=lambda n: (distance[n], -len(wanted & terms(_haystack(entities[n]))), n)
    )
    chosen, dropped = ranked[:max_entities], ranked[max_entities:]
    retrieval, deferred, spent = [], [], 0
    for name in chosen:
        source = entities[name].get("source")
        if not source:
            continue
        cost = estimate_tokens(source["size"]) if "size" in source else DEFAULT_SOURCE_TOKENS
        step = {
            "entity": name,
            "locator": source["locator"],
            "revision": source.get("revision"),
            "sha256": source.get("sha256"),
            "estimated_tokens": cost,
        }
        if spent + cost <= token_budget:
            retrieval.append(step)
            spent += cost
        else:
            deferred.append(step)
    return {
        "seeds": seeds,
        "entities": [{**entities[name], "distance": distance[name]} for name in chosen],
        "edges": [list(link) for link in links if link[0] in chosen and link[2] in chosen],
        "retrieval": retrieval,
        "deferred": deferred,
        "dropped": dropped,
        "frontier": sorted(frontier - set(chosen)),
        "bounds": {
            "hops": hops,
            "max_entities": max_entities,
            "token_budget": token_budget,
            "estimated_tokens": spent,
        },
    }
