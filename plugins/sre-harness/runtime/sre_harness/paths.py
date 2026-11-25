"""Where run data lives: outside every repository, private to the user."""

import os
import re
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .errors import BoundaryError, InputError

NAMESPACE = "sre-harness"
RUN_ID = re.compile(r"run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}")


def _outside_git(path):
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise BoundaryError(
                f"refused to store run data inside a git repository ({parent}). "
                "Point AI_HARNESS_HOME at a directory outside any repository."
            )


def _private_dirs(path):
    """Create the directory and any missing parents 0700. Existing directories are left alone."""
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)


def state_root(env=None):
    """`<ai-harness home>/sre-harness`. The home is shared with the improvement harness."""
    env = os.environ if env is None else env
    if env.get("AI_HARNESS_HOME"):
        home = Path(env["AI_HARNESS_HOME"])
    else:
        home = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ai-harness"
    root = Path(os.path.abspath(home.expanduser())) / NAMESPACE
    for candidate in (root.parent, root):
        if candidate.is_symlink():
            raise BoundaryError(f"refused to store run data behind a symlink: {candidate}")
    root = root.resolve()
    _outside_git(root)
    _private_dirs(root)
    os.chmod(root, 0o700)
    return root


def new_run_id(now=None):
    now = now or datetime.now(timezone.utc)
    return f"run-{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"


def create_run(root, run_id=None):
    """A run directory is always new. An existing one is never reused or overwritten."""
    run_id = run_id or new_run_id()
    if not RUN_ID.fullmatch(run_id):
        raise InputError(f"not a run id: {run_id!r}")
    _private_dirs(root / "runs")
    path = root / "runs" / run_id
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        raise InputError(f"run {run_id} already exists.") from None
    os.chmod(path, 0o700)
    return path


def open_run(root, run_id):
    """Resolve a run by id. A path is not accepted, so a run cannot point outside the root."""
    if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
        raise InputError(f"not a run id: {run_id!r}. `sre-harness runs list` shows the runs.")
    path = root / "runs" / run_id
    if path.is_symlink() or not path.is_dir():
        raise InputError(f"run {run_id} does not exist under {root / 'runs'}.")
    return path


def list_runs(root):
    runs = root / "runs"
    if not runs.is_dir():
        return []
    return sorted(child.name for child in runs.iterdir() if RUN_ID.fullmatch(child.name))


def child(directory, *parts):
    """A path below `directory`, built from names the harness chose (ids and hashes)."""
    path = directory.joinpath(*parts)
    for part in parts:
        if not part or part in {".", ".."} or "/" in part or "\\" in part or "\0" in part:
            raise BoundaryError(f"refused output name {part!r}.")
    return path


def write_private(path, text):
    """Atomic 0600 write: a crash leaves the old file or the new one, never half of either."""
    path = Path(path)
    _private_dirs(path.parent)
    if path.is_symlink():
        raise BoundaryError(f"refused to write through a symlink: {path}")
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
