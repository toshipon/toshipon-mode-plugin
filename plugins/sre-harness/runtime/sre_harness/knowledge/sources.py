"""Reading the text an evidence locator points at, for `knowledge fetch`. Nothing is fetched
from the network and nothing is read outside the state root.

    registry:RG-<n>/v<n>.md                       <root>/registry/<entry>/<version>.md
    run:<run id>/documents.jsonl#DOC-<n>#S<n>     one section of a run's documents.jsonl
    git:<path>[#L<a>[-L<b>]]                      the lines at the recorded commit, from the
                                                  bare repository `spec analyze` keeps for the
                                                  record's repo (<root>/cache/git/); the hash
                                                  covers the cited lines, as the project
                                                  context explorer stamps them
    pr:… doc:…                                    not read in this version

A locator is matched whole against these forms, so `..`, an absolute path, a backslash or a
NUL never reach a path. Each directory and the file are refused when they are symlinks, and
the file is opened with O_NOFOLLOW. Anything that does not resolve is `source_unavailable`;
the caller never gets a made-up text.
"""

import json
import os
import re
import stat
from dataclasses import dataclass

from .. import paths
from ..spec import prs

MAX_SOURCE_BYTES = 256 * 1024  # one evidence text; more is `too_large`, never cut
MAX_DOCUMENTS_BYTES = 64 * 1024 * 1024  # a run's documents.jsonl, read to find one section

SOURCE_UNAVAILABLE = "source_unavailable"
TOO_LARGE = "too_large"

_REGISTRY = re.compile(r"registry:(RG-[0-9]{4,})/(v[0-9]{4,}\.md)")
_RUN = re.compile(
    r"run:(run-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8})/documents\.jsonl#(DOC-[0-9]{4,})#(S[0-9]{1,6})"
)
_GIT = re.compile(
    r"git:(?P<path>[A-Za-z0-9_.@+=,-]+(?:/[A-Za-z0-9_.@+=,-]+)*)"
    r"(?:#L(?P<start>[1-9][0-9]{0,6})(?:-L(?P<end>[1-9][0-9]{0,6}))?)?"
)
_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"([A-Za-z0-9][A-Za-z0-9-]{0,38})/((?!\.{1,2}$)[A-Za-z0-9._-]{1,100})")
_CACHE_KEY = "[0-9a-f]{12}"


@dataclass(frozen=True)
class Read:
    """The text, or why there is none."""

    text: str | None = None
    reason: str | None = None


UNAVAILABLE = Read(reason=SOURCE_UNAVAILABLE)


def _regular_file(root, parts, cap):
    """The UTF-8 text of root/parts, or a Read with the reason it cannot be had."""
    path = root
    for part in parts:
        path = paths.child(path, part)
        if path.is_symlink():
            return UNAVAILABLE
    try:
        # O_NONBLOCK: a FIFO planted at the path is opened without waiting, then refused.
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
    except OSError:
        return UNAVAILABLE
    with os.fdopen(descriptor, "rb") as stream:
        status = os.fstat(stream.fileno())
        if not stat.S_ISREG(status.st_mode):
            return UNAVAILABLE
        if status.st_size > cap:
            return Read(reason=TOO_LARGE)
        data = stream.read(cap + 1)
    if len(data) > cap:
        return Read(reason=TOO_LARGE)
    try:
        return Read(text=data.decode("utf-8"))
    except UnicodeDecodeError:
        return UNAVAILABLE


def _registry(root, match):
    return _regular_file(root, ("registry", match[1], match[2]), MAX_SOURCE_BYTES)


def _section(rows, document, section):
    for row in rows:
        if not isinstance(row, dict) or row.get("id") != document:
            continue
        for part in row.get("sections") or ():
            if isinstance(part, dict) and part.get("id") == section:
                text = part.get("text")
                return text if isinstance(text, str) else None
    return None


def _run(root, match):
    found = _regular_file(root, ("runs", match[1], "documents.jsonl"), MAX_DOCUMENTS_BYTES)
    if found.text is None:
        return found
    try:
        # Only "\n" ends a row, as records.read_jsonl reads it.
        rows = [json.loads(line) for line in found.text.split("\n") if line.strip()]
    except json.JSONDecodeError:
        return UNAVAILABLE
    text = _section(rows, match[2], match[3])
    if text is None:
        return UNAVAILABLE
    if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
        return Read(reason=TOO_LARGE)
    return Read(text=text)


def _repositories(root, repo):
    """The bare repositories `spec analyze` made for `repo` (one per fetch source)."""
    match = _REPO.fullmatch(repo or "")
    base = root / "cache" / "git"
    if match is None or (root / "cache").is_symlink() or base.is_symlink() or not base.is_dir():
        return []
    name = re.compile(re.escape(f"{match[1]}--{match[2]}--") + _CACHE_KEY + r"\.git")
    return [
        folder
        for folder in sorted(base.iterdir())
        if name.fullmatch(folder.name)
        and not folder.is_symlink()
        and folder.is_dir()
        and (folder / "HEAD").is_file()
    ]


def _cited(text, start, end):
    """The lines a git locator names (as the project context explorer cites them), or None
    when the range is not in the file."""
    if start is None:
        return text
    first, last = int(start), int(end or start)
    lines = text.splitlines()
    if last < first or last > len(lines):
        return None
    return "\n".join(lines[first - 1 : last])


def _git(root, match, record, revision):
    if not _SHA.fullmatch(revision) or ".." in match["path"].split("/"):
        return UNAVAILABLE
    target = f"{revision}:{match['path']}"
    for git_dir in _repositories(root, record["facets"]["repo"]):
        repository = prs.Repository(git_dir, None)
        code, out = repository.run(["cat-file", "-s", target], allowed=range(0, 256))
        if code != 0:
            continue
        size = int(out.strip() or 0)
        if size > MAX_SOURCE_BYTES:
            return Read(reason=TOO_LARGE)
        code, out = repository.run(["cat-file", "blob", target], allowed=range(0, 256))
        if code != 0 or len(out) > MAX_SOURCE_BYTES:
            return UNAVAILABLE
        try:
            text = out.decode("utf-8")
        except UnicodeDecodeError:
            return UNAVAILABLE
        cited = _cited(text, match["start"], match["end"])
        return UNAVAILABLE if cited is None else Read(text=cited)
    return UNAVAILABLE


def read(root, record, evidence):
    """The text behind one evidence entry of `record`, or the reason there is none."""
    locator = evidence["locator"]
    match = _REGISTRY.fullmatch(locator)
    if match:
        return _registry(root, match)
    match = _RUN.fullmatch(locator)
    if match:
        return _run(root, match)
    match = _GIT.fullmatch(locator)
    if match:
        return _git(root, match, record, evidence["revision"])
    return UNAVAILABLE  # pr:, doc: and anything else are not read in this version
