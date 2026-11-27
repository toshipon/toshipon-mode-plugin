"""`knowledge export --format context-entities`: the approved records a principal may read, as
the entities the project context explorer's `index add --file` reads (its
`references/contract.md`, "Entity"). Nothing is written: the list goes to stdout.

The dependency runs one way, explorer <- harness files, as `index import-registry` already
does. The harness never reads the explorer's index: it holds descriptions an agent wrote, not
approved norms.

    records       approved, readable by the principal (the store's access filter), and not
                  replaced by an approved successor (`search.replacements`); with --project,
                  the organization's and that project's
    id            the record id (KN-...), which the explorer's id pattern takes
    type          decision for a decision, requirement for the rest (a rule, a requirement
                  and a policy check all say what a change must satisfy)
    description   the summary on one line, at most 240 characters
    status        approved
    source        the first git: evidence the harness itself reads, with its revision (a
                  40-digit sha), sha256 and size: both sides hash the cited lines alike.
                  registry:, run:, pr: and doc: give no source (the explorer reports
                  no_source), so nothing the explorer cannot check is passed on
    relationships the record's trace relations (refines, implements, verifies, depends_on,
                  conflicts_with) whose target is exported too; supersedes is not passed on,
                  since a replaced record is not exported
"""

from ..errors import BoundaryError, QualityGateError
from . import config, search, sources, store, untrusted

FORMAT = "context-entities"
MAX_DESCRIPTION = 240
DECISION = "decision"
REQUIREMENT = "requirement"


def _in_project(record, project):
    return project is None or record["scope"]["project_id"] in (None, project)


def _source(record):
    for evidence in record["evidence"]:
        if sources.reads_git(evidence["locator"], evidence["revision"]):
            return {
                "locator": evidence["locator"],
                "revision": evidence["revision"],
                "sha256": evidence["content_sha256"],
                "size": evidence["size"],
            }
    return None


def entity(record, exported):
    """One record as an explorer entity. `exported` is every id the export holds."""
    value = {
        "id": record["id"],
        "type": DECISION if record["kind"] == DECISION else REQUIREMENT,
        "description": untrusted.one_line(record["summary"], MAX_DESCRIPTION),
        "status": "approved",
        "relationships": sorted(
            (
                {"type": row["relation"], "to": row["target"]}
                for row in record.get("relations", ())
                if row["target"] in exported
            ),
            key=lambda row: (row["type"], row["to"]),
        ),
    }
    source = _source(record)
    if source is not None:
        value["source"] = source
    return value


def entities(rows, project=None):
    """The entities of the readable `rows`, by id. Pure."""
    replaced = search.replacements(rows)
    kept = [
        row
        for row in sorted(rows, key=lambda row: row["id"])
        if row["status"] == "approved" and row["id"] not in replaced and _in_project(row, project)
    ]
    exported = {row["id"] for row in kept}
    return [entity(row, exported) for row in kept]


def run(root, name, project=None):
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    principal = config.principal(settings, name)
    with store.reader(root) as reader:
        stale = reader.stale_sources(config.config_hash(settings))
        if stale:
            raise QualityGateError(
                "stale_index: these sources were taken in with another knowledge/config.json: "
                f"{', '.join(stale)}; run `sre-harness knowledge update` for each of them first. "
                "Nothing was exported."
            )
        return entities(reader.records(principal), project)
