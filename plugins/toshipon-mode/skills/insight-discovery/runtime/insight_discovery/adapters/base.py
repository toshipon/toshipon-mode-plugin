"""What every adapter shares: path refusals, size limits, and tolerant JSON loading."""

import json
import re
from pathlib import Path

from ..foundation import Refusal

IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,100}")
MAX_DEPTH = 32


class Unrecognized(Exception):
    """The file is not in a shape this adapter knows. Counted, never guessed at."""


def identifier(value):
    """The value when it is a plain identifier, else None. Free text never becomes a label."""
    return value if isinstance(value, str) and IDENTIFIER.fullmatch(value) else None


def safe_path(path):
    """An absolute path with no `..` and no link at its end. Refused otherwise."""
    raw = Path(str(path)).expanduser()
    if ".." in raw.parts:
        raise Refusal(f"path must not contain '..': {path}")
    if raw.is_symlink():
        raise Refusal(f"path must not be a symbolic link: {path}")
    return raw.absolute()


def listed(directory, patterns, limits, stats):
    """Regular files matching the patterns, sorted, at most max_files_per_source of them."""
    found = sorted({path for pattern in patterns for path in directory.glob(pattern)})
    files = []
    for path in found:
        if path.is_symlink() or not path.is_file():
            stats["skipped_link"] += 1
        else:
            files.append(path)
    limit = limits["max_files_per_source"]
    stats["deferred_files"] += max(0, len(files) - limit)
    return files[:limit]


def read_text(path, limits):
    """The file's text. An oversized file is refused whole, never cut short."""
    if path.stat().st_size > limits["max_file_bytes"]:
        raise Refusal(f"file exceeds max_file_bytes and was not read: {path.name}")
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise Unrecognized("not UTF-8 text") from None


def _depth(value, level=1):
    if level > MAX_DEPTH:
        raise Unrecognized("nested too deeply")
    children = value.values() if isinstance(value, dict) else value
    if isinstance(value, (dict, list)):
        for child in children:
            _depth(child, level + 1)


def load_object(path, limits):
    """A JSON object from the file, or Unrecognized."""
    try:
        value = json.loads(read_text(path, limits))
        _depth(value)
    except (ValueError, RecursionError):
        raise Unrecognized("not JSON, or nested too deeply") from None
    if not isinstance(value, dict):
        raise Unrecognized("not a JSON object")
    return value
