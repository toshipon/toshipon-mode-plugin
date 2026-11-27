"""Git, read-only. Every call goes through `git`, which refuses anything but a fixed set of
subcommands and any option that writes a file or runs a configured program."""

import os
import re
import subprocess

from .base import Refusal
from .untrusted import invisible

READ_ONLY = frozenset({"rev-parse", "show", "cat-file", "diff", "log", "ls-tree"})
_WRITING_OPTION = re.compile(
    r"^--(?:output|ext-diff|textconv|exec|upload-pack|open-files-in-pager)\b"
)
REVISION = re.compile(r"^[0-9a-f]{7,40}$")
_NEVER_AN_OPTION = re.compile(r"^[^-]")


def git(root, *args):
    """stdout of a read-only git subcommand, or None when git exits non-zero."""
    if not args or args[0] not in READ_ONLY:
        raise Refusal(f"git subcommand is not read-only: {args[:1]}")
    if any(_WRITING_OPTION.match(arg) for arg in args):
        raise Refusal("git option can write a file or run a program")
    # A repository's own config can name programs. A repository that claims to be a partial
    # clone makes git fetch a missing object, which starts the configured upload-pack. Reading
    # never needs a fetch, so fetching and every transport are switched off.
    env = {
        **os.environ,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_LAZY_FETCH": "1",
        "LC_ALL": "C",
    }
    for configured_program in ("GIT_EXTERNAL_DIFF", "GIT_DIFF_OPTS", "GIT_PAGER"):
        env.pop(configured_program, None)
    done = subprocess.run(
        ["git", "-C", str(root), "--no-pager", "-c", "protocol.allow=never", *args],
        capture_output=True,
        env=env,
        check=False,
    )
    if done.returncode != 0:
        return None
    return done.stdout.decode("utf-8", errors="replace")


def checked_revision(revision):
    if not isinstance(revision, str) or not REVISION.match(revision):
        raise Refusal(f"revision must be a commit sha (7-40 hex): {revision!r}")
    return revision


def checked_path(path):
    """A repository-relative path: no absolute path, no `..`, nothing git reads as an option."""
    parts = path.split("/") if isinstance(path, str) else [""]
    if (
        not path
        or path.startswith("/")
        or ".." in parts
        or "" in parts
        or any(map(invisible, path))
    ):
        raise Refusal(f"path must be relative to the repository root: {path!r}")
    if not _NEVER_AN_OPTION.match(path) or ":" in path:
        raise Refusal(f"path is not a plain repository path: {path!r}")
    return path


def resolve(root, revision):
    """The full sha of a commit, or None when the repository does not have it."""
    out = git(root, "rev-parse", "--verify", "--quiet", f"{checked_revision(revision)}^{{commit}}")
    return out.strip() if out else None


def head(root):
    out = git(root, "rev-parse", "--verify", "--quiet", "HEAD^{commit}")
    if not out:
        raise Refusal(f"not a git repository with a commit: {root}")
    return out.strip()


def show(root, revision, path):
    """The file's text at the revision, or None when the revision has no file at the path."""
    name = f"{checked_revision(revision)}:{checked_path(path)}"
    if (git(root, "cat-file", "-t", name) or "").strip() != "blob":
        return None
    return git(root, "show", "--no-ext-diff", "--no-textconv", name)


def diff(root, base, head_revision, path=None):
    args = ["diff", "--no-ext-diff", "--no-textconv", "--no-color", "-U3"]
    args.append(f"{checked_revision(base)}...{checked_revision(head_revision)}")
    if path:
        args += ["--", checked_path(path)]
    return git(root, *args)
