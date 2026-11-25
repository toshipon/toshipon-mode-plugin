"""The versioned registry of decided candidates, `<state root>/registry/`, apart from any run.

    registry/RG-0001/v0001.json   the decision (schema review-registry)
    registry/RG-0001/v0001.md     the artifact as it was decided
    registry/RG-0001/v0001.diff   the activation diff, when one was asked for

An entry follows one candidate (run id + candidate id). Every decision writes a new version and
an existing file is never replaced. The latest version decides whether the entry is active.
"""

import os
import re

from .. import paths, records
from ..errors import InputError

DIRECTORY = "registry"
ENTRY_ID = re.compile(r"RG-[0-9]{4,}")
VERSION_FILE = re.compile(r"v([0-9]{4,})\.json")


def version_name(version, suffix):
    return f"v{version:04d}.{suffix}"


def _entry_dir(root, entry_id):
    if not isinstance(entry_id, str) or not ENTRY_ID.fullmatch(entry_id):
        raise InputError(f"not a registry id: {entry_id!r}")
    return paths.child(root, DIRECTORY, entry_id)


def entries(root):
    """{entry id: [version records, oldest first]}."""
    base = paths.child(root, DIRECTORY)
    found = {}
    if not base.is_dir():
        return found
    for folder in sorted(base.iterdir()):
        if folder.is_symlink() or not folder.is_dir() or not ENTRY_ID.fullmatch(folder.name):
            continue
        versions = []
        for path in sorted(folder.iterdir()):
            if VERSION_FILE.fullmatch(path.name) and not path.is_symlink():
                versions.append(records.validate("review-registry", records.read_json(path)))
        versions.sort(key=lambda row: row["version"])
        if [row["version"] for row in versions] != list(range(1, len(versions) + 1)):
            raise InputError(f"registry entry {folder.name} has a gap in its versions.")
        if versions:
            found[folder.name] = versions
    return found


def lineage_of(found, run_id, candidate_id):
    for entry_id, versions in found.items():
        lineage = versions[0]["lineage"]
        if (lineage["run_id"], lineage["candidate_id"]) == (run_id, candidate_id):
            return entry_id
    return None


def next_id(found):
    numbers = [int(entry_id.split("-")[1]) for entry_id in found]
    return records.record_id("RG", max(numbers, default=0) + 1)


def _create(path, text):
    """A version file is written once; an existing one is never replaced."""
    if path.exists() or path.is_symlink():
        raise InputError(f"registry file {path.name} already exists; nothing was replaced.")
    paths.write_private(path, text)


def write_version(root, record, artifact, diff=None):
    folder = _entry_dir(root, record["id"])
    records.validate("review-registry", record)
    version = record["version"]
    _create(paths.child(folder, version_name(version, "md")), artifact)
    if diff is not None:
        _create(paths.child(folder, version_name(version, "diff")), diff)
    _create(
        paths.child(folder, version_name(version, "json")),
        records.canonical(record) + "\n",
    )
    os.chmod(folder, 0o700)
    return folder


def artifact(root, record):
    path = paths.child(_entry_dir(root, record["id"]), record["candidate"]["artifact_file"])
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        raise InputError(f"registry artifact {record['id']}/{path.name} is unreadable.") from None


def active_rules(root, repo):
    """The latest version of every entry for `repo` that is approved, as rules to evaluate."""
    rules = []
    for entry_id, versions in entries(root).items():
        latest = versions[-1]
        if latest["status"] != "approved" or latest["repo"] != repo:
            continue
        candidate = latest["candidate"]
        rule = {
            "id": entry_id,
            "title": candidate["title"],
            "problem": candidate["problem"],
            "form": candidate["form"],
            "body": artifact(root, latest),
            "content_sha256": candidate["content_sha256"],
        }
        if "check" in candidate:
            rule["check"] = candidate["check"]
        rules.append(rule)
    return rules
