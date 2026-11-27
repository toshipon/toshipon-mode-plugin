"""The task-context contract: what a task runner hands the harness about one piece of work.

`knowledge context`, `knowledge check` and `knowledge decision put` read it from --task-file.
The file is external input: it is read once, capped, decoded and checked against the
`task-context` schema here, and nothing after this module looks at it again. It is only read.

The task id and the workspace id are names without meaning to the harness; nothing here
parses a ticket id or a tool out of them. `request` turns a task into the one search it
describes, so `knowledge context` is `knowledge search` with its arguments taken from the file.
"""

import json
import os
import stat

from .. import records
from ..errors import InputError
from . import search

SCHEMA = "task-context"
MAX_BYTES = 256 * 1024


def read_capped(path, cap, what):
    """The bytes of the regular file at `path`, at most `cap` of them (exit 2 otherwise)."""
    try:
        # O_NONBLOCK: a FIFO given as the file is opened without waiting, then refused.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        raise InputError(f"cannot read the {what} {path}: {error.strerror}") from None
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise InputError(f"the {what} {path} is not a regular file.")
        data = stream.read(cap + 1)
    if len(data) > cap:
        raise InputError(f"the {what} {path} is larger than {cap} bytes; nothing was read.")
    return data


def load(path):
    """The checked task context in `path` (exit 2 when it is not one)."""
    data = read_capped(path, MAX_BYTES, "task file")
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise InputError(f"the task file {path} is not UTF-8 JSON.") from None
    problems = records.violations(SCHEMA, value)
    if problems:
        raise InputError(f"the task file {path} is not a task-context: " + "; ".join(problems[:5]))
    return value


def request(task, *, query="", max_tokens=search.DEFAULT_MAX_TOKENS, paths=()):
    """The search a task describes. `paths` are added to its changed paths (`knowledge check`
    adds the paths its patch touches)."""
    return search.Request(
        project=task["project_id"],
        component=task["component_id"],
        task=task["task_id"],
        query=query,
        repo=task["repo"],
        paths=tuple(sorted(set(task["changed_paths"]) | set(paths))),
        resource_types=tuple(task["resource_types"]),
        max_tokens=max_tokens,
    )
