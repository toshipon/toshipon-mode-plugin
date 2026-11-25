import json
import os
import subprocess
import sys
from pathlib import Path

LAUNCHER = Path(__file__).resolve().parents[3] / "scripts" / "sre-harness"


def test_the_launcher_finds_its_runtime_from_any_directory(tmp_path):
    env = {
        "PATH": os.environ["PATH"],
        "SRE_HARNESS_PYTHON": sys.executable,
        "AI_HARNESS_HOME": str(tmp_path / "state"),
    }
    result = subprocess.run(
        [str(LAUNCHER), "runs", "list"], env=env, cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runs"] == []
