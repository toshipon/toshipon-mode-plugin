#!/usr/bin/env python3
"""Builds the synthetic project as a git repository with two commits: `project/` as the base,
then `change/` on top as the head (the change a synthetic PR #42 makes).

    python3 synthetic_repo.py <empty directory>     prints {"root", "base", "head"}
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Synthetic Author",
    "GIT_AUTHOR_EMAIL": "author@example.invalid",
    "GIT_COMMITTER_NAME": "Synthetic Author",
    "GIT_COMMITTER_EMAIL": "author@example.invalid",
    "GIT_AUTHOR_DATE": "2026-03-01T00:00:00+00:00",
    "GIT_COMMITTER_DATE": "2026-03-01T00:00:00+00:00",
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_SYSTEM": os.devnull,
}


def _git(root, *args):
    done = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, **_IDENTITY},
    )
    return done.stdout.strip()


def commit(root, message):
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def build_base(root):
    root = Path(root)
    shutil.copytree(HERE / "project", root, dirs_exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    return commit(root, "synthetic: base")


def apply_change(root):
    shutil.copytree(HERE / "change", Path(root), dirs_exist_ok=True)
    return commit(root, "synthetic: PR #42 changes retention and visibility timeout")


def build(root):
    base = build_base(root)
    return {"root": str(Path(root).resolve()), "base": base, "head": apply_change(root)}


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    print(json.dumps(build(sys.argv[1]), indent=2))
