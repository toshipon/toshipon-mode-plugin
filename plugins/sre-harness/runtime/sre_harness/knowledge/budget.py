"""The task total: how many tokens `knowledge search` and `knowledge fetch` returned for one
task, counted across calls and sessions.

    <state root>/knowledge/tasks/<hash>.jsonl    one ledger per (task id, principal), 0600

The file name is a hash of the task id and the principal, so a task id (a string with no
meaning to the harness) never becomes a path. A ledger is only appended to. Each call holds
an exclusive flock on it while it reads the total, decides and appends, and appends its lines
with one write, so two sessions on the same task neither break a line nor both pass the limit.

A line is either

    call    command, record ids, tokens charged (0 when refused), tokens needed, whether it
            was charged, the index generation, the time
    extend  by how much, the reason given, the principal, the time

and never a summary, a source text or a query.

The rule: a call is refused when what it would return, added to what the task already used,
is over the limit (the default plus every extension). A refused call returns nothing and
charges nothing; nothing is cut to fit.
"""

import fcntl
import json
import os
from dataclasses import dataclass

from .. import paths, records
from ..errors import BoundaryError, InputError
from . import store

DEFAULT_LIMIT = 6000
DIRECTORY = "tasks"
NAME_LENGTH = 32
_READ_CHUNK = 64 * 1024


@dataclass(frozen=True)
class Task:
    id: str
    extend_by: int | None = None
    reason: str | None = None


@dataclass(frozen=True)
class Charge:
    allowed: bool
    view: dict  # budget.task of the result: {id, limit, used_before, used_after[, needed_tokens]}


def ledger(root, task_id, principal):
    name = records.sha256(records.canonical([task_id, principal]))[:NAME_LENGTH] + ".jsonl"
    return paths.child(store.directory(root), DIRECTORY, name)


def _private_directory(path):
    """`path` as a 0700 directory, made here or by a concurrent call. A symlink is refused."""
    try:
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    except FileExistsError:
        pass
    if path.is_symlink() or not path.is_dir():
        raise BoundaryError(f"refused to use {path} for the task total: not a plain directory.")
    return path


def _folder(root):
    return _private_directory(paths.child(_private_directory(store.directory(root)), DIRECTORY))


def _valid(row):
    if not isinstance(row, dict):
        return False
    count = (
        "tokens" if row.get("event") == "call" else "by" if row.get("event") == "extend" else None
    )
    value = row.get(count) if count else None
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse(data, path):
    rows = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw.decode("utf-8"))  # strict: bytes that are not UTF-8 are damage
        except (UnicodeDecodeError, json.JSONDecodeError):
            row = None
        if not _valid(row):
            raise InputError(
                f"the task ledger {path.name} has a damaged line; nothing was counted. "
                "Move the file away to start the task total again."
            )
        rows.append(row)
    return rows


def _read_all(descriptor):
    os.lseek(descriptor, 0, os.SEEK_SET)
    chunks = []
    while True:
        chunk = os.read(descriptor, _READ_CHUNK)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def entries(root, task_id, principal):
    """The ledger's lines, oldest first; empty when the task has none yet."""
    path = ledger(root, task_id, principal)
    if path.is_symlink():
        raise BoundaryError(f"refused to read a task ledger through a symlink: {path}")
    if not path.exists():
        return []
    return _parse(path.read_bytes(), path)


def _totals(rows, limit):
    extended = sum(row["by"] for row in rows if row["event"] == "extend")
    used = sum(row["tokens"] for row in rows if row["event"] == "call")
    return limit + extended, used


def _append(descriptor, rows):
    data = "".join(records.canonical(row) + "\n" for row in rows).encode("utf-8")
    while data:  # a regular file takes it in one write; the lock covers a short one
        data = data[os.write(descriptor, data) :]


def charge(root, task, principal, *, command, ids, tokens, generation, now, limit=DEFAULT_LIMIT):
    """Count one call of `command` that would return `tokens`. Returns whether it may."""
    path = paths.child(_folder(root), ledger(root, task.id, principal).name)
    flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as error:
        raise BoundaryError(f"refused to open the task ledger {path}: {error.strerror}") from None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        os.fchmod(descriptor, 0o600)
        rows = _parse(_read_all(descriptor), path)
        written = []
        if task.extend_by:
            written.append(
                {
                    "event": "extend",
                    "task": task.id,
                    "principal": principal,
                    "by": task.extend_by,
                    "reason": task.reason,
                    "at": now,
                }
            )
        ceiling, used = _totals(rows + written, limit)
        allowed = used + tokens <= ceiling
        written.append(
            {
                "event": "call",
                "task": task.id,
                "principal": principal,
                "command": command,
                "ids": list(ids),
                "tokens": tokens if allowed else 0,
                "needed": tokens,
                "charged": allowed,
                "generation": generation,
                "at": now,
            }
        )
        _append(descriptor, written)
    finally:
        os.close(descriptor)  # closing releases the lock
    view = {
        "id": task.id,
        "limit": ceiling,
        "used_before": used,
        "used_after": used + tokens if allowed else used,
    }
    if not allowed:
        view["needed_tokens"] = tokens
    return Charge(allowed, view)
