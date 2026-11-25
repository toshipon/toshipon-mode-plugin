"""Synthetic stand-in for the candidate step: edits the skill inside the isolated worktree (cwd)."""

import json
import sys
from pathlib import Path

json.load(sys.stdin)
skill = Path("skill.md")
skill.write_text(
    skill.read_text(encoding="utf-8") + "- 完了を報告する前に、主張ごとの evidence を添える。\n",
    encoding="utf-8",
)
print(json.dumps({"summary": "added the evidence rule"}))
