"""Regression guard: workspace snapshots, own-change detection, safe revert, baseline checks."""

import fnmatch
import os
import subprocess
from pathlib import Path

from .evaluation import DEFAULT_TIMEOUT, run_command
from .redact import redact_text
from .store import ACTIVE_ENV, HarnessError, sha256, write_private

OUTPUT_TAIL = 1000


def _git(project, *args):
    """Every git call: paths are literal (never globs), repository hooks do not run, and the
    child carries the recursion guard like any other process the harness starts."""
    return subprocess.run(
        ["git", "--literal-pathspecs", "-c", "core.hooksPath=/dev/null", "-C", str(project), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, ACTIVE_ENV: "1"},
    )


def git(project, *args, check=True):
    done = _git(project, *args)
    if check and done.returncode:
        raise HarnessError(f"git {args[0]} failed: {done.stderr.strip()[:300]}")
    return done.stdout


def require_git(project):
    if git(project, "rev-parse", "--is-inside-work-tree", check=False).strip() != "true":
        raise HarnessError(
            "the project must be a git work tree; own changes cannot be reverted safely otherwise"
        )


def _content_hash(project, path):
    target = Path(project) / path
    if target.is_symlink() or not target.is_file():
        return None
    return sha256(target.read_bytes())


def _dirty_paths(project):
    """Paths git reports as modified, staged, deleted or untracked (both sides of a rename)."""
    raw = git(project, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    tokens = [token for token in raw.split("\0") if token]
    paths, index = set(), 0
    while index < len(tokens):
        status, path = tokens[index][:2], tokens[index][3:]
        paths.add(path)
        if status[0] in "RC":
            index += 1
            paths.add(tokens[index])
        index += 1
    return paths


def snapshot(project):
    """What was already uncommitted before the experiment. These files belong to the user."""
    return {
        "head": git(project, "rev-parse", "HEAD").strip(),
        "dirty": {path: _content_hash(project, path) for path in sorted(_dirty_paths(project))},
    }


def changes(project, before, protected):
    """Files the experiment touched, and anything that makes an automatic revert unsafe."""
    violations = []
    if git(project, "rev-parse", "HEAD").strip() != before["head"]:
        violations.append({"kind": "head_moved", "path": ""})
    own = sorted(_dirty_paths(project) - set(before["dirty"]))
    for path, content in before["dirty"].items():
        if _content_hash(project, path) != content:
            violations.append({"kind": "preexisting_modified", "path": path})
    for path in own:
        if any(fnmatch.fnmatch(path, pattern) for pattern in protected):
            violations.append({"kind": "protected_path", "path": path})
    return own, violations


def _in_head(project, path):
    return _git(project, "cat-file", "-e", f"HEAD:{path}").returncode == 0


def save_patch(project, own, destination):
    """Store the experiment's diff (redacted). Returns the hash of the unredacted patch."""
    tracked = [path for path in own if _in_head(project, path)]
    patch = git(project, "diff", "HEAD", "--", *tracked) if tracked else ""
    for path in own:
        if path not in tracked:
            patch += git(project, "diff", "--no-index", "--", "/dev/null", path, check=False)
    write_private(destination, redact_text(patch))
    return sha256(patch)


def revert(project, own):
    """Undo only the experiment's own files.

    None of them was dirty at the snapshot, so each either matched HEAD or did not exist.
    """
    for path in own:
        if _in_head(project, path):
            git(project, "checkout", "HEAD", "--", path)
            continue
        git(project, "reset", "-q", "HEAD", "--", path, check=False)
        target = Path(project) / path
        if target.is_file() or target.is_symlink():
            target.unlink()


def run_checks(spec, project):
    results = []
    for check in spec.get("guard", {}).get("checks", []):
        result = run_command(
            check["command"], project, check.get("timeout_seconds", DEFAULT_TIMEOUT)
        )
        output = (
            result["stdout"][-OUTPUT_TAIL:] + "\n" + result["stderr_tail"][-OUTPUT_TAIL:]
        ).strip()
        results.append(
            {
                "name": check["name"],
                "passed": result["exit_code"] == 0,
                "exit_code": result["exit_code"],
                "output_tail": redact_text(output),
            }
        )
    return results
