"""Synthetic stand-in for the hypothesis step of the harness-evolution agent."""

import json
import sys

pattern = json.load(sys.stdin)["pattern"]
print(
    json.dumps(
        {
            "cause": "skill",
            "statement": "skill に完了報告へ evidence を添える指示を足すと、根拠不足による差し戻しが減る",
            "falsification": "holdout の first_pass_rate が 0.2 以上上がらない、または既存ケースが落ちる",
            "risks": [f"{pattern['signature']} 以外の失敗には効かない"],
        },
        ensure_ascii=False,
    )
)
