"""`<state root>/knowledge/config.json`: projects and principals, written by a person.

A principal reads a record when the record's classification is one of the principal's and
the record shares a reader with it. Deciding that for stored records is the store's
`access_clause`; this module only resolves who the principal is. It is not authentication:
anyone who can read the state root can read the index.
"""

from dataclasses import dataclass

from .. import records
from ..errors import BoundaryError
from . import store

FILE = "config.json"
CLASSIFICATIONS = ("public", "synthetic", "internal", "confidential")


@dataclass(frozen=True)
class Principal:
    name: str
    readers: frozenset
    classifications: frozenset


def load(root):
    """The checked config, or None when there is none."""
    path = store.directory(root) / FILE
    if path.is_symlink():
        raise BoundaryError(f"refused to read the knowledge config through a symlink: {path}")
    if not path.exists():
        return None
    return records.validate("knowledge-config", records.read_json(path))


def known_readers(config):
    """Every reader some principal holds. A record naming any other reader is not indexed."""
    return frozenset(
        reader for principal in config["principals"].values() for reader in principal["readers"]
    )


def project_of(config, repo):
    """(project id, project) holding `repo`, or (None, the reason it has none)."""
    found = [
        (name, project) for name, project in config["projects"].items() if repo in project["repos"]
    ]
    if not found:
        return None, "repo_not_in_config"
    if len(found) > 1:
        return None, "repo_in_several_projects"
    return found[0]


def principal(config, name):
    entry = config["principals"].get(name)
    if entry is None:
        # The known names are not listed: they are part of the configuration, not the answer.
        raise BoundaryError(f"unknown principal {name!r}; it is not in knowledge/config.json.")
    return Principal(name, frozenset(entry["readers"]), frozenset(entry["classifications"]))


def resolve(root, name):
    """The principal named `name`. There is no default: no config means no principal (exit 3)."""
    config = load(root)
    if config is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    return principal(config, name)


def permissions_hash(principal):
    """What the principal may read, without its name: equal hashes read equal records."""
    return records.fingerprint(
        {"readers": sorted(principal.readers), "classifications": sorted(principal.classifications)}
    )


def config_hash(config):
    return records.fingerprint(config)
