"""Synthetic suite runner: a case passes when the skill under test contains the guidance it needs.

Stands in for "run the agent with this harness version on this case". Deterministic and local.
"""

import json
import os
from pathlib import Path

skill = (Path(os.environ["AI_HARNESS_VERSION_DIR"]) / "skill.md").read_text(encoding="utf-8")
case = json.loads(Path(os.environ["AI_HARNESS_CASE_PATH"]).read_text(encoding="utf-8"))
passed = case["needs"] in skill
print(
    json.dumps(
        {
            "passed": passed,
            "required": {"skill_has_title": skill.startswith("# ")},
            "quality": 1.0 if passed else 0.4,
            "fix_count": 0 if passed else 2,
            "tokens": len(skill),
            "cost": 0,
        }
    )
)
