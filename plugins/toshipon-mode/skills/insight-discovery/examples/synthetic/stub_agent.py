#!/usr/bin/env python3
"""Stand-in for the model, for the demo and the tests. It needs no account and costs nothing.

It reads one stage payload on stdin and prints a well-formed answer derived from it. It follows
the deterministic signals it is shown, so it exercises the mechanism (checks, routing, ranking).
It does not demonstrate that a real model finds the shared structure.
"""

import json
import sys

PROBLEM = "上流の入力の妥当性が下流の工程で初めて分かり、下流の作業をやり直している"


def observe(body):
    texts = {item["id"]: item.get("text") for item in body["evidence"]}
    observations = []
    for signal in body["signals"]:
        quotes = [texts[ref] for ref in signal["evidence_ids"] if texts[ref]][:1]
        observations.append(
            {
                "text": f"{signal['label']} が {signal['occurrences']} 回、"
                f"{len(signal['contexts'])} 種の業務で記録されている",
                "evidence_refs": signal["evidence_ids"],
                "quotes": quotes,
            }
        )
    interpretations = [
        {
            "text": "同じ種類の出来事が、業務の種類をまたいで繰り返している可能性がある",
            "based_on": [0],
        }
    ]
    return {
        "observations": observations,
        "interpretations": interpretations if observations else [],
    }


def structure(body):
    return {
        "groups": [
            {
                "observations": [item["id"]],
                "abstracted_problem": PROBLEM,
                "levels": {
                    "individual": "個々のやり直しは、入力の不備を直せば解消する",
                    "structural": "検査が、不備の影響を受ける工程より後ろに置かれている",
                    "premise": "検査は成果物が出来てから行うもの、という前提に立っている",
                },
                "doubted_premise": "入力の妥当性は、処理を最後まで進めないと判定できない",
            }
            for item in body["observations"]
            if len(item["task_keys"]) >= 2
        ]
    }


def hypothesize(body):
    hypotheses = []
    for group in body["groups"]:
        hypotheses += [
            {
                "group": group["id"],
                "statement": "下流が必要とする入力の条件を上流の入口で先に検査すると、やり直しの回数が減る",
                "generator": "inversion",
                "falsification": "入口の検査を足しても、やり直しの回数が足す前と変わらない",
                "counter_evidence": [
                    "入口では判定できない不備（後工程の状態に依存するもの）がある"
                ],
            },
            {
                "group": group["id"],
                "statement": "製造の受入検査のように、工程ごとに受け取る側が合否条件を宣言する形にすると、不備が見つかる位置が前に動く",
                "generator": "analogy",
                "analogy": {
                    "source_domain": "製造業の受入検査",
                    "mapping": "後工程が受入条件を示し、前工程の出口で判定する",
                    "where_it_breaks": "文書や設計のように合否を機械的に判定しにくい成果物",
                },
                "falsification": "受入条件を宣言しても、不備が見つかる工程の位置が変わらない",
                "counter_evidence": [],
            },
        ]
    return {"hypotheses": hypotheses}


def audit(body):
    return {
        "audits": [
            {
                "hypothesis": item["id"],
                "existing_sufficient": {
                    "verdict": "no",
                    "basis": "成果物の改善でも作業手法の改善でもなく、検査をどこに置くかという問題設定を扱っている",
                    "refs": [],
                },
                "problem_framing": {
                    "verdict": "sound",
                    "basis": "観測が複数の業務にまたがっている",
                },
                "falsifiable": {"verdict": "yes", "basis": "やり直しの回数と発見位置は数えられる"},
                "findings": [],
                "recommendation": "keep",
            }
            for item in body["hypotheses"]
        ],
        "limitations": ["合成データだけを見ている"],
    }


def assess(body):
    assessments = []
    for index, item in enumerate(body["hypotheses"]):
        scored = {"score": 3 - index % 3, "reason": "合成データ上の仮の評価"}
        assessments.append(
            {
                "hypothesis": item["id"],
                "scores": {
                    "value": scored,
                    "delta_from_prior_art": {
                        "score": 2,
                        "reason": "既存手法を調べていないので中間に置く",
                    },
                    "testability": {"score": 3, "reason": "回数を数えるだけで判定できる"},
                    "cost": {"score": 2, "reason": "1 業務で半日程度"},
                },
                "minimal_experiment": {
                    "procedure": "1 つの業務で入口の検査を 1 つ足し、2 週間のやり直し回数を数える",
                    "comparison": "検査を足す前の 2 週間のやり直し回数",
                    "estimated_hours": 4,
                },
                "success_criteria": "やり直し回数が半分以下になる",
                "kill_criteria": "やり直し回数が変わらない、または検査の追加で所要時間が増える",
                "estimated_cost": {"hours": 4, "money": 0, "note": "人の作業時間だけ"},
                "confidence": "low",
                "confidence_reason": "合成データで、観測期間も短い",
                "unverified": ["やり直しの原因が入力の不備だけかどうか"],
            }
        )
    return {"assessments": assessments}


STAGES = {
    "observe": observe,
    "structure": structure,
    "hypothesize": hypothesize,
    "audit": audit,
    "assess": assess,
}


def answer(body):
    return STAGES[body["stage"]](body)


if __name__ == "__main__":
    json.dump(answer(json.load(sys.stdin)), sys.stdout, ensure_ascii=False)
