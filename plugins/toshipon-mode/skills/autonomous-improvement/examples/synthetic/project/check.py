"""Synthetic regression check: fails when settings.json says the tests fail."""

import json
import sys
from pathlib import Path

settings = json.loads(Path(__file__).with_name("settings.json").read_text(encoding="utf-8"))
sys.exit(0 if settings["tests_pass"] else 1)
