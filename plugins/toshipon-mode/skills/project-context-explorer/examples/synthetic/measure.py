#!/usr/bin/env python3
"""Measures the two things that can be measured without people, on the synthetic project only:

- retrieval precision and recall of `explore` against gold.json (hand labels), at the default bounds
- whether the claim check agrees with the hand labels in claims.json (seeded unsupported claims)

    python3 measure.py          prints JSON

Time to understanding needs people and real reviews. Nothing here measures it."""

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1] / "runtime"))

import synthetic_repo  # noqa: E402

from context_recovery import index as ix  # noqa: E402
from context_recovery.claims import verify  # noqa: E402
from context_recovery.evidence import Evidence  # noqa: E402
from context_recovery.traverse import explore  # noqa: E402


def _load(name):
    return json.loads((HERE / name).read_text(encoding="utf-8"))


def retrieval(index):
    rows = []
    for item in _load("gold.json")["questions"]:
        found = [entity["id"] for entity in explore(index, question=item["question"])["entities"]]
        hits = sorted(set(found) & set(item["relevant"]))
        rows.append(
            {
                "question": item["question"],
                "retrieved": len(found),
                "relevant": len(item["relevant"]),
                "hits": len(hits),
                "precision": round(len(hits) / len(found), 2) if found else None,
                "recall": round(len(hits) / len(item["relevant"]), 2),
                "missed": sorted(set(item["relevant"]) - set(found)),
            }
        )
    retrieved, hits = sum(r["retrieved"] for r in rows), sum(r["hits"] for r in rows)
    relevant = sum(r["relevant"] for r in rows)
    return {
        "questions": len(rows),
        "bounds": {"hops": 1, "max_entities": 12},
        "micro_precision": round(hits / retrieved, 2) if retrieved else None,
        "micro_recall": round(hits / relevant, 2),
        "per_question": rows,
    }


def claim_check(root, base, head, store):
    text = (HERE / "claims.json").read_text(encoding="utf-8")
    claims = json.loads(text.replace("{{BASE}}", base).replace("{{HEAD}}", head))["claims"]
    expected = {claim["id"]: claim.pop("expected") for claim in claims}
    report = verify(claims, Evidence(root, store).resolve)
    verdict = {
        r["id"]: "not_asserting"
        if r["status"] == "unknown"
        else "supported"
        if r["supported"]
        else "unsupported"
        for r in report["results"]
    }
    seeded = [name for name, label in expected.items() if label == "unsupported"]
    sound = [name for name, label in expected.items() if label == "supported"]
    return {
        "claims": report["claims"],
        "asserting": report["asserting"],
        "seeded_unsupported": len(seeded),
        "seeded_unsupported_caught": sum(verdict[name] == "unsupported" for name in seeded),
        "supported_wrongly_rejected": sum(verdict[name] != "supported" for name in sound),
        "unsupported_claim_rate_of_fixture": report["unsupported_claim_rate"],
    }


def measure():
    work = Path(tempfile.mkdtemp(prefix="context-explorer-measure-")).resolve()
    try:
        repo = work / "tidepool"
        repo.mkdir()
        os.environ.setdefault("AI_HARNESS_HOME", str(work / "state"))
        base = synthetic_repo.build_base(repo)
        index = ix.upsert(ix.empty(repo), _load("entities.json"), repo)
        head = synthetic_repo.apply_change(repo)
        return {
            "data": "synthetic (examples/synthetic); labels by hand; no real project, no people",
            "retrieval": retrieval(index),
            "claim_check": claim_check(repo, base, head, work / "evidence"),
            "time_to_understanding": "not measured",
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    print(json.dumps(measure(), ensure_ascii=False, indent=2))
