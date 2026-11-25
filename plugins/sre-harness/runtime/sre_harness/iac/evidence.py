"""Read one evidence file below the --evidence directory, and nothing outside it.

A locator is a relative path of plain names. Absolute paths, `..`, `.`, empty parts and
backslashes are refused without touching the disk. No symlink is followed, on the way or at
the end. A file over MAX_BYTES is not read.
"""

import os
import stat
from pathlib import Path

MAX_BYTES = 16 * 1024 * 1024


def _refused(locator):
    if not isinstance(locator, str) or not locator or "\\" in locator or "\0" in locator:
        return True
    if locator.startswith("/"):
        return True
    return any(part in {"", ".", ".."} for part in locator.split("/"))


def read(root, locator):
    """(bytes, None) when the file is read, or (None, violation code)."""
    if _refused(locator):
        return None, "evidence_path_refused"
    current = Path(root)
    info = None
    for part in locator.split("/"):
        current = current / part
        try:
            info = os.lstat(current)
        except (FileNotFoundError, NotADirectoryError):
            return None, "evidence_missing"
        if stat.S_ISLNK(info.st_mode):
            return None, "evidence_symlink"
    if info is None or not stat.S_ISREG(info.st_mode):
        return None, "evidence_missing"
    if info.st_size > MAX_BYTES:
        return None, "evidence_too_large"
    descriptor = os.open(current, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        return None, "evidence_too_large"
    return data, None
