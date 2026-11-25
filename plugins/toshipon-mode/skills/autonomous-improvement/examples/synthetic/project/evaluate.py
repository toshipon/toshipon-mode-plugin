"""Synthetic evaluator adapter: reads settings.json and prints metrics as JSON."""

import json
from pathlib import Path

settings = json.loads(Path(__file__).with_name("settings.json").read_text(encoding="utf-8"))
if settings.get("broken"):
    raise SystemExit("synthetic evaluator failure")
print(
    json.dumps(
        {
            "metrics": {
                "throughput": settings["cache_entries"] * 2,
                "valid": int(settings["valid"]),
            },
            "evidence": [{"id": "settings", "ref": "settings.json"}],
        }
    )
)
