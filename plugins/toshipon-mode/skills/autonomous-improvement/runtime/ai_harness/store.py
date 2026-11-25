"""Private state home: location, permissions, locks, schema checks and hash-chained logs."""

import contextlib
import copy
import fcntl
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .redact import redact

SCHEMA_DIR = Path(__file__).resolve().parents[2] / "schemas"
ACTIVE_ENV = "AI_HARNESS_ACTIVE"  # set for every child process; hooks and dispatch refuse under it
AUTOMATED_ENV = "AI_HARNESS_AUTOMATED"  # set by scheduled entry points; human gates refuse under it
GENESIS = "0" * 64
RESERVED_REVIEWERS = ("claude", "ai", "bot", "agent", "assistant", "harness", "gpt", "llm")

DEFAULT_CONFIG = {
    "schema_version": 1,
    "collect": {"raw": False},
    "retention_days": 30,
    "queue": {
        "max_attempts": 3,
        "backoff_base_seconds": 60,
        "backoff_max_seconds": 3600,
        "lease_seconds": 1800,
    },
    "budget": {"max_jobs_per_dispatch": 5, "max_seconds_per_dispatch": 3600, "max_cost_per_day": 5},
    "schedule": [
        {"kind": "evolve", "hour": 2, "minute": 0},
        {"kind": "weekly_report", "weekday": 0, "hour": 9, "minute": 0},
    ],
    "evolution": {"min_occurrences": 3, "min_distinct_sources": 2},
    "llm_policy": {"allowed_classifications": ["public", "synthetic"]},
    "agents": {},
    "publish": {"allow_paths": []},
    "privacy": {},
}


class HarnessError(Exception):
    """A refusal the caller can act on. The CLI prints it and exits 2."""


class Busy(HarnessError):
    """Another process holds the lock."""


def utcnow():
    fixed = os.environ.get("AI_HARNESS_NOW")
    return datetime.fromisoformat(fixed) if fixed else datetime.now(timezone.utc)


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def parse_time(text):
    return datetime.fromisoformat(text)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256(text):
    data = text if isinstance(text, bytes) else text.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def state_home(env=None):
    env = os.environ if env is None else env
    if env.get("AI_HARNESS_HOME"):
        return ensure_home(env["AI_HARNESS_HOME"])
    base = env.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return ensure_home(Path(base) / "ai-harness")


def ensure_home(path):
    """Create the state home 0700. Run records never live inside a repository."""
    path = Path(os.path.abspath(Path(path).expanduser()))
    if path.is_symlink():
        raise HarnessError(f"state home must not be a symlink: {path}")
    path = path.resolve()
    for parent in (path, *path.parents):
        if (parent / ".git").exists():
            raise HarnessError(f"state home must be outside any git repository: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path, 0o700)
    for child in path.iterdir():  # repair modes loosened since the last run
        if not child.is_symlink():
            os.chmod(child, 0o700 if child.is_dir() else 0o600)
    return path


def private_dir(path):
    """Create the directory and any missing parents 0700. Existing directories are left alone."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for directory in reversed(missing):
        directory.mkdir(mode=0o700)
        os.chmod(directory, 0o700)
    return missing[0] if missing else path


def write_private(path, text):
    """Atomic 0600 write: a crash leaves the old file or the new one, never half of either."""
    path = Path(path)
    private_dir(path.parent)
    handle, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def write_json(path, value):
    write_private(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def read_json(path, default=None) -> Any:
    path = Path(path)
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


@contextlib.contextmanager
def locked(path, blocking=True):
    path = Path(path)
    private_dir(path.parent)
    handle = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise Busy(f"locked by another process: {path.name}") from None
        yield
    finally:
        os.close(handle)


_validators = {}


def _registry():
    documents = [
        json.loads(path.read_text(encoding="utf-8")) for path in SCHEMA_DIR.glob("*.schema.json")
    ]
    pairs = [(document["$id"], Resource.from_contents(document)) for document in documents]
    return Registry().with_resources(pairs)


def validate(name, value: Any) -> Any:
    """Validate against schemas/<file>.schema.json, or one of its $defs as '<file>#<def>'."""
    if name not in _validators:
        filename, _, definition = name.partition("#")
        reference = f"autonomous-improvement/{filename}.schema.json"
        if definition:
            reference += f"#/$defs/{definition}"
        _validators[name] = Draft202012Validator({"$ref": reference}, registry=_registry())
    errors = sorted(_validators[name].iter_errors(value), key=lambda e: list(map(str, e.path)))
    if errors:
        where = "/".join(str(part) for part in errors[0].absolute_path) or "(root)"
        raise HarnessError(f"{name} schema violation at {where}: {errors[0].message[:300]}")
    return value


def load_config(home):
    """User config merged over defaults, one level deep. A missing file means all defaults."""
    config = copy.deepcopy(DEFAULT_CONFIG)
    user = read_json(Path(home) / "config.json", {})
    validate("config", user)
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict):
            config[key].update(value)
        else:
            config[key] = value
    return config


def require_human(reviewer, reason, env=None):
    """Human gates are a procedural control, not authentication: refuse the obvious bypasses."""
    env = os.environ if env is None else env
    if env.get(AUTOMATED_ENV) or env.get(ACTIVE_ENV):
        raise HarnessError("human approval cannot be recorded from an automated run")
    name = (reviewer or "").strip()
    # Split on case and digit boundaries too: "ClaudeCode", "GPT4" and "OpenAIBot" name machines,
    # while "Claudette" and "Aiko" stay whole words.
    words = [
        word.lower() for word in re.findall(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[^\W\d_A-Za-z]+", name)
    ]
    if not name or any(word in RESERVED_REVIEWERS for word in words):
        raise HarnessError("reviewer must name a person; AI and automation names are refused")
    if not (reason or "").strip():
        raise HarnessError("approval needs a reason")
    return {"reviewer": name, "reason": reason.strip()}


def _digest(record):
    return sha256(canonical({key: value for key, value in record.items() if key != "hash"}))


def read_log(path):
    """All records of a chained log. A torn final line (crash mid-append) is ignored."""
    path = Path(path)
    if not path.exists():
        return []
    lines = path.read_bytes().split(b"\n")
    lines.pop()  # text after the last newline: empty when clean, a torn record otherwise
    records, previous = [], GENESIS
    for number, line in enumerate(lines, start=1):
        try:
            record = json.loads(line)
        except ValueError:
            raise HarnessError(
                f"{path.name} line {number} is not JSON; the log is corrupt"
            ) from None
        if (
            record.get("seq") != number
            or record.get("prev") != previous
            or record.get("hash") != _digest(record)
        ):
            raise HarnessError(
                f"{path.name} line {number} breaks the hash chain; the log was edited"
            )
        records.append(record)
        previous = record["hash"]
    return records


def append_log(path, log_id, record_type, data, now):
    """Append one redacted, schema-checked record under a lock and return it."""
    path = Path(path)
    with locked(path.with_suffix(".lock")):
        if path.exists():
            raw = path.read_bytes()
            clean = raw.rfind(b"\n") + 1
            if clean != len(raw):
                os.truncate(path, clean)
        records = read_log(path)
        record = {
            "schema_version": 1,
            "id": f"{log_id}:{len(records) + 1}",
            "run_id": log_id,
            "seq": len(records) + 1,
            "created_at": iso(now),
            "type": record_type,
            "data": redact(data),
            "prev": records[-1]["hash"] if records else GENESIS,
        }
        record["hash"] = _digest(record)
        validate("ledger", record)
        private_dir(path.parent)
        handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(handle, "ab") as stream:
            stream.write((canonical(record) + "\n").encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
    return record
