"""`knowledge migrate` and `knowledge rollback`: take the review registry into the index after a
backup, checked before anything is replaced; and put a backup back.

migrate gives what a backup followed by `knowledge update --source review-registry` would,
but the index in place changes only when the result agrees with the registry:

    1. under the writer lock, copy the index in place (or start an empty one) in a temporary
       file beside it (`store.staged`)
    2. take the registry in on the copy as `update --source review-registry` does. The records
       of the other sources (`file:` and the rest) stay as they are
    3. check, for every repo of knowledge/config.json but those several projects hold (they
       have no one project, so the update skips their entries; listed in `check.excluded`), that
       the entries
       `registry.active_rules(root, repo)` returns are exactly the approved records the copy
       holds from the review-registry source for that repo. A repo the registry itself
       refuses (an approved artifact changed after it was recorded) is a difference. Any
       difference: exit 1, nothing is replaced and nothing is backed up
    4. back up the index in place to knowledge/backups/ and put the copy in its place with one
       rename. A crash leaves the old index or the new one

`--dry-run` reads the registry and the index and writes nothing: every entry's record id,
scope, access and enforcement, the unmapped and the invalid entries, and what the update
would change.

rollback puts a backup back the same way: the index in place is backed up first. The restored
index holds the backup's records under the next generation, not the backup's own number, so
that one generation never names two contents.

The registry is only read here. `review evaluate` keeps reading it directly. No model is
called, and every migrated record is advisory.
"""

from .. import records
from ..errors import EXIT_OK, EXIT_QUALITY, HarnessError
from ..review import registry
from . import config, ingest, store
from .adapters import review_registry

SOURCE = review_registry.NAME
CHECKER = "migrate-check"


def _everyone(settings):
    """A principal reading every record the index can hold (every known reader and every
    classification). It checks the migration; it never answers a search."""
    return config.Principal(
        CHECKER, config.known_readers(settings), frozenset(config.CLASSIFICATIONS)
    )


SHARED = "repo_in_several_projects"


def repos(settings):
    return sorted({repo for project in settings["projects"].values() for repo in project["repos"]})


def excluded(settings):
    """The repos left out of the check, with why: a repo that several projects hold has no one
    project, so its entries are never taken in (`skipped` with the same reason)."""
    return [
        {"repo": repo, "reason": SHARED}
        for repo in repos(settings)
        if config.project_of(settings, repo)[1] == SHARED
    ]


def expected(root, settings):
    """{repo: ids of the entries `registry.active_rules` returns, or why it refused them}, for
    the repos the check covers (not `excluded`)."""
    left_out = {row["repo"] for row in excluded(settings)}
    found = {}
    for repo in repos(settings):
        if repo in left_out:
            continue
        try:
            found[repo] = {rule["id"] for rule in registry.active_rules(root, repo)}
        except HarnessError as error:
            found[repo] = str(error)
    return found


def indexed(reader, settings):
    """{repo: entry ids of the approved review-registry records} of the index `reader` reads."""
    principal = _everyone(settings)
    sources = reader.provenance(principal)
    found = {}
    for row in reader.records(principal):
        if sources.get(row["id"]) == SOURCE and row["status"] == "approved":
            found.setdefault(row["facets"]["repo"], set()).add(row["origin"]["key"])
    return found


def differences(wanted, held):
    """One row per repo of `wanted` ({repo: entry ids, or an error}) against `held`. Pure."""
    rows = []
    for repo in sorted(wanted):
        have = set(held.get(repo, ()))
        if isinstance(wanted[repo], str):
            ids, missing, unexpected, error = set(), set(), set(), wanted[repo]
        else:
            ids, error = set(wanted[repo]), None
            missing, unexpected = ids - have, have - ids
        rows.append(
            {
                "repo": repo,
                "ok": error is None and not missing and not unexpected,
                "registry": sorted(ids),
                "index": sorted(have),
                "missing": sorted(missing),
                "unexpected": sorted(unexpected),
                "error": error,
            }
        )
    return rows


def _planned(prepared):
    rows = [
        {
            "entry": record["origin"]["key"],
            "version": record["origin"]["version"],
            "id": record["id"],
            "kind": record["kind"],
            "status": record["status"],
            "scope": record["scope"],
            "access": record["access"],
            "enforcement": record["enforcement"],
        }
        for record in prepared.accepted
    ]
    skipped = prepared.changes.skipped or []
    return {
        "records": sorted(rows, key=lambda row: row["entry"]),
        "unmapped": [row for row in skipped if row["kind"] == review_registry.UNMAPPED],
        "invalid": [row for row in skipped if row["kind"] == review_registry.INVALID],
        "rejected": prepared.rejected,
    }


def _answer(prepared, summary, *, dry_run, backup, check, replaced):
    return {
        "source": SOURCE,
        "dry_run": dry_run,
        "plan": _planned(prepared),
        "changes": {
            name: summary[name] for name in ("inserted", "updated", "unchanged", "tombstoned")
        },
        "generation": summary["generation"],
        "backup": backup,
        "check": check,
        "replaced": replaced,
    }


def plan(root, now=None):
    """The dry run: what a migration would do, read without writing a byte."""
    store.require_fts5()
    settings = ingest.settings(root)
    now = now or records.iso()
    adapter = review_registry.ReviewRegistryAdapter(root)
    with store.peek(root) as current:
        previous = None if current is None else current.cursor(SOURCE)
        prepared = ingest.prepare(adapter, settings, previous, now)
        summary = store.plan_on(current, prepared.batch)
    skipped = {"name": None, "reason": "dry_run"}
    return _answer(prepared, summary, dry_run=True, backup=skipped, check=None, replaced=False)


def run(root, now=None):
    """Migrate (steps 1-4 of the module doc). Exit 1 (`exit_code`) when the check fails."""
    store.require_fts5()
    settings = ingest.settings(root)
    now = now or records.iso()
    adapter = review_registry.ReviewRegistryAdapter(root)
    with store.staged(root) as stage:
        with stage.read() as current:
            previous = current.cursor(SOURCE)
        prepared = ingest.prepare(adapter, settings, previous, now)
        summary = stage.writer.apply(prepared.batch, now)
        with stage.read() as built:
            rows = differences(expected(root, settings), indexed(built, settings))
        check = {
            "ok": all(row["ok"] for row in rows),
            "repos": rows,
            "excluded": excluded(settings),
        }
        if not check["ok"]:
            kept = {"name": None, "reason": "not_replaced"}
            return _answer(
                prepared, summary, dry_run=False, backup=kept, check=check, replaced=False
            )
        saved = stage.commit(now)
    backup = saved or {"name": None, "reason": "no_index"}
    return _answer(prepared, summary, dry_run=False, backup=backup, check=check, replaced=True)


def rollback(root, name, now=None):
    """Put the backup `name` in place of the index, after backing the index up."""
    now = now or records.iso()
    with store.staged(root, start=name) as stage:
        with stage.read() as restored:
            generation = restored.generation
        before = stage.current
        after = stage.advance(now)
        saved = stage.commit(now)
    return {
        "restored": {"name": name, "generation": generation},
        "generation": {"before": before or 0, "after": after},
        "backup": saved or {"name": None, "reason": "no_index"},
    }


def backups(root):
    return store.backups(root)


def exit_code(result):
    return EXIT_OK if result["dry_run"] or result["replaced"] else EXIT_QUALITY
