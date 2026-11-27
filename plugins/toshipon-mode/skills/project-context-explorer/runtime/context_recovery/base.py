"""Shared ground: the state home outside any repository, private writes, hashing, estimates."""

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
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def append_private(path, record):
    path = Path(path)
    private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def read_json(path, what="file"):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise Refusal(f"{what} not found: {path}") from None
    except (UnicodeDecodeError, json.JSONDecodeError) as broken:
        raise Refusal(f"{what} is not UTF-8 JSON: {path}: {broken}") from None


def read_jsonl(path):
    path = Path(path)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def dump(value):
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
