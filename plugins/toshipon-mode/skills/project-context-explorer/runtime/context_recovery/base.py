"""Shared ground: the state home outside any repository, private writes, hashing, estimates.

The state home follows the rules of `ai_harness.store` in the Autonomous Improvement skill.
That module is not imported, because it needs the jsonschema package and these skills run on
the standard library alone."""

import hashlib
import json
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_VERSION = 1


class Refusal(Exception):
    """Input or state the command will not act on. `problems` lists every reason found."""

    def __init__(self, message, problems=None):
        super().__init__(message)
        self.problems = list(problems or [])


def now():
    fixed = os.environ.get("AI_HARNESS_NOW")
    moment = datetime.fromisoformat(fixed) if fixed else datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def sha256(text):
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def estimate_tokens(size_bytes):
    """A deliberately high estimate: 3 UTF-8 bytes per token covers English and Japanese."""
    return math.ceil(size_bytes / 3)


def state_home(env=None):
    """The same home the improvement harness uses. Records never live inside a repository."""
    env = os.environ if env is None else env
    if env.get("AI_HARNESS_HOME"):
        home = Path(env["AI_HARNESS_HOME"]).expanduser()
    else:
        home = Path(env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state") / "ai-harness"
    home = Path(os.path.abspath(home))
    if home.is_symlink():
        raise Refusal(f"state home must not be a symlink: {home}")
    home = home.resolve()
    for parent in (home, *home.parents):
        if (parent / ".git").exists():
            raise Refusal(f"state home must be outside any git repository: {home}")
    return home


def project_dir(kind, root, *parts, env=None):
    """<home>/<kind>/<root name>-<hash of its real path>/<parts...>, created 0700."""
    real = Path(root).resolve()
    path = state_home(env) / kind / f"{real.name or 'root'}-{sha256(str(real))[:8]}"
    for part in parts:
        path = path / part
    for directory in (path, *path.parents):
        if directory == state_home(env):
            break
        if directory.is_symlink():  # a planted link would move the records somewhere else
            raise Refusal(f"state directory must not be a symlink: {directory}")
    return private_dir(path)


def private_dir(path):
    """Create the directory and every missing parent 0700. `mkdir(parents=True)` would leave
    the parents at the umask's mode. Directories that already exist are left alone."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    return Path(missing[0]) if missing else path


def write_private(path, text):
    """Replace the file atomically, readable by the owner only."""
    path = Path(path)
    private_dir(path.parent)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def append_private(path, record, create=False):
    """Append one record. With `create`, the file must not exist yet: of two processes that
    start the same log, one gets FileExistsError."""
    path = Path(path)
    private_dir(path.parent)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | (os.O_EXCL if create else 0)
    descriptor = os.open(path, flags, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_text(path, what="file"):
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        raise Refusal(f"{what} not found: {path}") from None
    except UnicodeDecodeError:
        raise Refusal(f"{what} is not UTF-8 text: {path}") from None
    except OSError as failed:
        raise Refusal(f"{what} cannot be read: {path}: {failed.strerror}") from None


def read_json(path, what="file"):
    try:
        value = json.loads(read_text(path, what))
        json.dumps(value, ensure_ascii=False).encode("utf-8")  # a lone surrogate cannot be stored
    except json.JSONDecodeError as broken:
        raise Refusal(f"{what} is not UTF-8 JSON: {path}: {broken}") from None
    except UnicodeEncodeError:
        raise Refusal(f"{what} holds a character that is not valid Unicode text: {path}") from None
    return value


def read_jsonl(path):
    """Every record of an append-only log. A line that does not parse (a write cut short)
    is refused by line number; nothing is skipped silently."""
    path = Path(path)
    if not path.exists():
        return []
    records = []
    for number, line in enumerate(read_text(path, "log").splitlines(), 1):
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            raise Refusal(f"log has a broken record at line {number}: {path}") from None
    return records


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
