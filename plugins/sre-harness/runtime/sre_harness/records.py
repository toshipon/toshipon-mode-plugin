"""Shared record plumbing: canonical JSON, hashes, ids, schema validation, JSONL files."""

import hashlib
import json
import re
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from .errors import InputError
from .paths import write_private

SCHEMA_VERSION = 1
SCHEMA_DIR = Path(__file__).resolve().parent.parent / "schemas"
RECORD_ID = re.compile(r"[A-Z]{2,4}-[0-9]{4,}")


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def sha256(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()


def fingerprint(value):
    """Hash of a JSON value, stable across key order."""
    return sha256(canonical(value))


def iso(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def record_id(prefix, number):
    """`RT-0001`: an upper-case prefix and at least four digits, as in the improvement harness."""
    if not re.fullmatch(r"[A-Z]{2,4}", prefix) or isinstance(number, bool) or number < 1:
        raise ValueError("record ids need an upper-case prefix and a positive number")
    return f"{prefix}-{number:04d}"


@lru_cache(maxsize=1)
def _registry():
    resources = []
    for path in sorted(SCHEMA_DIR.glob("*.schema.json")):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@lru_cache(maxsize=None)
def _validator(name):
    path = SCHEMA_DIR / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=_registry())


def schema(name):
    return json.loads((SCHEMA_DIR / f"{name}.schema.json").read_text(encoding="utf-8"))


def violations(name, value, definition=None):
    """Human-readable schema violations, empty when the value conforms."""
    validator = _validator(name)
    if definition:
        reference = {"$ref": f"urn:sre-harness:{name}#/$defs/{definition}"}
        validator = validator.evolve(schema=reference)
    errors = sorted(validator.iter_errors(value), key=lambda error: list(error.absolute_path))
    return [
        f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: {error.message[:200]}"
        for error in errors
    ]


def validate(name, value, definition=None):
    problems = violations(name, value, definition)
    if problems:
        raise InputError(f"{name} record is invalid: " + "; ".join(problems[:5]))
    return value


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise InputError(f"missing file: {path}") from None
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise InputError(f"{path} is not valid JSON: {error}") from None


def write_json(path, value):
    return write_private(
        path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def read_jsonl(path):
    try:
        # Only "\n" ends a row: splitlines() also cuts at U+2028, NEL and form feed.
        lines = Path(path).read_text(encoding="utf-8").split("\n")
    except FileNotFoundError:
        raise InputError(f"missing file: {path}") from None
    try:
        return [json.loads(line) for line in lines if line.strip()]
    except json.JSONDecodeError as error:
        raise InputError(f"{path} is not valid JSONL: {error}") from None


def write_jsonl(path, rows):
    return write_private(path, "".join(canonical(row) + "\n" for row in rows))
