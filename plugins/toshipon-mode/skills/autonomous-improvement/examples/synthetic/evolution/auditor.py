"""Synthetic stand-in for the independent auditor: reports limits, raises no finding."""

import json
import sys

json.load(sys.stdin)
print(
    json.dumps(
        {"findings": [], "limitations": ["合成ケースだけで測った。実作業での効果は未確認"]},
        ensure_ascii=False,
    )
)
