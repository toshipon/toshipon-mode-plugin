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


def test_modules_in_the_working_directory_do_not_run_inside_the_harness(tmp_path):
    # The user runs the harness from their own repository; files there must stay data.
    marker = tmp_path / "shadowed"
    planted = f"open({str(marker)!r}, 'a').write(__name__ + '\\n')\n"
    work = tmp_path / "repo"
    (work / "jsonschema").mkdir(parents=True)
    for name in ("argparse.py", "json.py", "jsonschema/__init__.py", "sre_harness.py"):
        (work / name).write_text(planted)
    env = {
        "PATH": os.environ["PATH"],
        "SRE_HARNESS_PYTHON": sys.executable,
        "AI_HARNESS_HOME": str(tmp_path / "state"),
        "PYTHONPATH": str(work),
    }
    result = subprocess.run(
        [str(LAUNCHER), "runs", "list"], env=env, cwd=work, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runs"] == []
    assert not marker.exists(), marker.read_text()
