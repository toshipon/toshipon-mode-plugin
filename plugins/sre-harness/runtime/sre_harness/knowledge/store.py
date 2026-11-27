"""The knowledge index on disk, and the only module that speaks SQL.

    <state root>/knowledge/index.sqlite3   the index (WAL; rebuilt from the sources at will)
    <state root>/knowledge/writer.lock     held with flock by the one writer

One writer at a time: `writer()` takes the lock without waiting, and a second writer stops with
exit 1 before it writes anything. One update is one `BEGIN IMMEDIATE` transaction; when it
changes anything, the generation goes up by one. A reader opens the file read-only and runs in
one read transaction, so it sees one generation from start to end, even while a writer is in
the middle of its transaction.

Which records a principal may read is decided in one place, `access_clause`. Every reader
method that returns records takes the principal first and puts that clause in its WHERE.
"""

import fcntl
import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field

from .. import paths
from ..errors import BoundaryError, InputError, QualityGateError
from . import model, terms

DIRECTORY = "knowledge"
INDEX = "index.sqlite3"
LOCK = "writer.lock"
SCHEMA_VERSION = 1
SUPERSEDES = "supersedes"
CONFLICTS_WITH = "conflicts_with"
TIMEOUT_SECONDS = 5

_SCHEMA = [
    "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE records (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        origin_kind TEXT NOT NULL,
        origin_key TEXT NOT NULL,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        enforcement TEXT NOT NULL,
        level TEXT NOT NULL,
        project_id TEXT,
        component_id TEXT,
        task_id TEXT,
        classification TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        record_hash TEXT NOT NULL,
        generation INTEGER NOT NULL,
        body TEXT NOT NULL
    )""",
    "CREATE INDEX records_by_source ON records (source)",
    "CREATE INDEX records_by_scope ON records (project_id, component_id, task_id)",
    """CREATE TABLE record_readers (
        id TEXT NOT NULL, reader TEXT NOT NULL, PRIMARY KEY (id, reader)
    ) WITHOUT ROWID""",
    """CREATE VIRTUAL TABLE record_terms USING fts5(
        id UNINDEXED, terms, tokenize = "unicode61 tokenchars '_'"
    )""",
    """CREATE TABLE edges (
        source_id TEXT NOT NULL, target_id TEXT NOT NULL, relation TEXT NOT NULL,
        PRIMARY KEY (source_id, target_id, relation)
    ) WITHOUT ROWID""",
    """CREATE TABLE tombstones (
        id TEXT PRIMARY KEY, origin TEXT NOT NULL, source TEXT NOT NULL, reason TEXT NOT NULL,
        generation INTEGER NOT NULL, at TEXT NOT NULL
    )""",
    """CREATE TABLE sources (
        name TEXT PRIMARY KEY, adapter TEXT NOT NULL, cursor TEXT, state TEXT NOT NULL,
        last_success_at TEXT NOT NULL
    )""",
]


@dataclass(frozen=True)
class Batch:
    """One update from one source. `records` are normalized and checked; `tombstones` are
    {id, origin, reason} the source reported; `rejected` are ids of items that failed the
    checks this time, so an older version of them must leave the index."""

    source: str
    adapter: str
    cursor: str | None
    complete: bool
    records: list = field(default_factory=list)
    tombstones: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    config_hash: str | None = None


# Files ------------------------------------------------------------------------------------


def directory(root, create=False):
    """`<root>/knowledge`, 0700. A symlink there is refused (exit 3)."""
    path = paths.child(root, DIRECTORY)
    if path.is_symlink():
        raise BoundaryError(f"refused to use a knowledge directory behind a symlink: {path}")
    if create and not path.exists():
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    return path


def _file(folder, name):
    path = paths.child(folder, name)
    if path.is_symlink():
        raise BoundaryError(f"refused to open the knowledge {name} through a symlink: {path}")
    return path


def _create_private(path, flags):
    try:
        return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except OSError as error:
        raise BoundaryError(f"refused to open {path}: {error.strerror}") from None


def exists(root):
    return (directory(root) / INDEX).is_file()


# SQLite and FTS5 --------------------------------------------------------------------------


def sqlite_version():
    return sqlite3.sqlite_version


def fts5_available():
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE VIRTUAL TABLE probe USING fts5(x)")
        return True
    except sqlite3.OperationalError:
        return False
    finally:
        connection.close()


def require_fts5():
    """There is no other ranking to fall back to: without FTS5 the index is not built."""
    if not fts5_available():
        raise InputError(
            f"the SQLite {sqlite_version()} of this Python has no FTS5, which the knowledge "
            "index needs. Use a Python whose sqlite3 module is built with FTS5."
        )


def _marks(values):
    return ", ".join("?" for _ in values)


def access_clause(principal):
    """The one decision of what a principal reads, as a WHERE fragment over `records` and its
    bound parameters: the classification is one the principal holds, and the record names at
    least one of the principal's readers. Values are never put into the SQL text."""
    classifications = sorted(principal.classifications)
    readers = sorted(principal.readers)
    if not classifications or not readers:
        return "0", []
    clause = (
        f"records.classification IN ({_marks(classifications)}) AND EXISTS ("
        "SELECT 1 FROM record_readers WHERE record_readers.id = records.id "
        f"AND record_readers.reader IN ({_marks(readers)}))"
    )
    return clause, [*classifications, *readers]


def _meta(connection):
    rows = dict(connection.execute("SELECT key, value FROM meta").fetchall())
    version = int(rows.get("schema_version", 0))
    if version != SCHEMA_VERSION:
        raise InputError(
            f"the knowledge index has schema version {version}, this harness uses "
            f"{SCHEMA_VERSION}. The index can be rebuilt: delete knowledge/{INDEX} and run "
            "`sre-harness knowledge update` again."
        )
    return {
        "schema_version": version,
        "generation": int(rows["generation"]),
        "config_hash": rows.get("config_hash"),
        "updated_at": rows.get("updated_at"),
    }


def _has_schema(connection):
    found = connection.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone()
    return found is not None


def _create_schema(connection):
    connection.execute("BEGIN IMMEDIATE")
    try:
        if not _has_schema(connection):
            for statement in _SCHEMA:
                connection.execute(statement)
            connection.executemany(
                "INSERT INTO meta (key, value) VALUES (?, ?)",
                [("schema_version", str(SCHEMA_VERSION)), ("generation", "0")],
            )
        connection.execute("COMMIT")
    except BaseException:
        connection.execute("ROLLBACK")
        raise


# Planning an update -------------------------------------------------------------------------


@dataclass
class _Plan:
    before: int
    config_changed: bool
    upserts: list  # records to write
    inserted: list
    updated: list
    unchanged: int
    deletes: dict  # id -> (origin, reason)

    @property
    def advanced(self):
        return bool(self.upserts or self.deletes or self.config_changed)

    @property
    def after(self):
        return self.before + 1 if self.advanced else self.before

    def summary(self):
        return {
            "generation": {"before": self.before, "after": self.after},
            "advanced": self.advanced,
            "config_changed": self.config_changed,
            "inserted": sorted(self.inserted),
            "updated": sorted(self.updated),
            "unchanged": self.unchanged,
            "tombstoned": [
                {"id": record_id, "reason": reason}
                for record_id, (_, reason) in sorted(self.deletes.items())
            ],
        }


def _plan(connection, batch):
    """What `batch` would change, read inside the caller's transaction. Refuses (exit 2) an
    update after which `supersedes` would have a cycle."""
    meta = _meta(connection)
    current = {
        row[0]: {"source": row[1], "hash": row[2], "origin": {"kind": row[3], "key": row[4]}}
        for row in connection.execute(
            "SELECT id, source, record_hash, origin_kind, origin_key FROM records"
        )
    }
    incoming = {record["id"]: record for record in batch.records}
    upserts, inserted, updated, unchanged = [], [], [], 0
    for record_id, record in sorted(incoming.items()):
        now = current.get(record_id)
        if now is None:
            inserted.append(record_id)
        elif now["hash"] != model.record_hash(record) or now["source"] != batch.source:
            updated.append(record_id)
        else:
            unchanged += 1
            continue
        upserts.append(record)

    def owned(record_id):
        found = current.get(record_id)
        return found is not None and found["source"] == batch.source and record_id not in incoming

    deletes = {}
    for stone in batch.tombstones:
        if owned(stone["id"]):
            deletes[stone["id"]] = (stone["origin"], stone["reason"])
    for record_id in batch.rejected:
        if owned(record_id):
            deletes.setdefault(record_id, (current[record_id]["origin"], "rejected"))
    if batch.complete:
        for record_id in sorted(current):
            if owned(record_id):
                deletes.setdefault(record_id, (current[record_id]["origin"], "removed_from_source"))

    edges = {}
    for source_id, target_id in connection.execute(
        "SELECT source_id, target_id FROM edges WHERE relation = ?", (SUPERSEDES,)
    ):
        edges.setdefault(source_id, []).append(target_id)
    for record_id in deletes:
        edges.pop(record_id, None)
    for record in upserts:
        edges[record["id"]] = list(record["supersedes"])
    cycle = model.supersession_cycle(edges)
    if cycle is not None:
        raise InputError(
            "records supersede each other in a cycle: "
            + " -> ".join(cycle)
            + ". Fix the source. Nothing was written."
        )
    return _Plan(
        before=meta["generation"],
        config_changed=meta["config_hash"] != batch.config_hash,
        upserts=upserts,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        deletes=deletes,
    )


def _searchable_text(record):
    facets = record["facets"]
    parts = [record["summary"], *record["applies_when"], *record["exceptions"]]
    parts += [*facets["topic"], *facets["resource_type"], *facets["path_globs"]]
    parts += [value for value in (facets["repo"], facets["decision_id"]) if value]
    return "\n".join(parts)


def _index_terms(connection, record):
    connection.execute(
        "INSERT INTO record_terms (id, terms) VALUES (?, ?)",
        (record["id"], " ".join(terms.terms(_searchable_text(record)))),
    )


def _forget(connection, record_id):
    connection.execute("DELETE FROM records WHERE id = ?", (record_id,))
    connection.execute("DELETE FROM record_readers WHERE id = ?", (record_id,))
    connection.execute("DELETE FROM record_terms WHERE id = ?", (record_id,))
    connection.execute("DELETE FROM edges WHERE source_id = ?", (record_id,))


def _write(connection, batch, plan, now):
    generation = plan.after
    for record_id, (origin, reason) in sorted(plan.deletes.items()):
        _forget(connection, record_id)
        connection.execute(
            "INSERT OR REPLACE INTO tombstones (id, origin, source, reason, generation, at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (record_id, json.dumps(origin, sort_keys=True), batch.source, reason, generation, now),
        )
    for record in plan.upserts:
        _forget(connection, record["id"])
        scope = record["scope"]
        connection.execute(
            "INSERT INTO records (id, source, origin_kind, origin_key, kind, status, enforcement, "
            "level, project_id, component_id, task_id, classification, content_hash, "
            "record_hash, generation, body) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record["id"],
                batch.source,
                record["origin"]["kind"],
                record["origin"]["key"],
                record["kind"],
                record["status"],
                record["enforcement"],
                scope["level"],
                scope["project_id"],
                scope["component_id"],
                scope["task_id"],
                record["access"]["classification"],
                record["content_hash"],
                model.record_hash(record),
                generation,
                json.dumps(record, ensure_ascii=False, sort_keys=True),
            ),
        )
        connection.executemany(
            "INSERT INTO record_readers (id, reader) VALUES (?, ?)",
            [(record["id"], reader) for reader in record["access"]["readers"]],
        )
        _index_terms(connection, record)
        connection.executemany(
            "INSERT INTO edges (source_id, target_id, relation) VALUES (?, ?, ?)",
            [(record["id"], target, SUPERSEDES) for target in record["supersedes"]],
        )
        connection.execute("DELETE FROM tombstones WHERE id = ?", (record["id"],))
    connection.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        [
            ("generation", str(generation)),
            ("config_hash", batch.config_hash or ""),
            ("updated_at", now),
        ],
    )


# Writer and reader --------------------------------------------------------------------------


class Writer:
    def __init__(self, connection):
        self._connection = connection

    def apply(self, batch, now):
        """Apply one update in one transaction. Anything raised inside rolls all of it back."""
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            plan = _plan(connection, batch)
            if plan.advanced:
                _write(connection, batch, plan, now)
            connection.execute(
                "INSERT OR REPLACE INTO sources (name, adapter, cursor, state, last_success_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    batch.source,
                    batch.adapter,
                    batch.cursor,
                    "complete" if batch.complete else "partial",
                    now,
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return plan.summary()


def _connect(path):
    """A read-write connection to an index file, in autocommit (transactions are explicit)."""
    return sqlite3.connect(str(path), isolation_level=None, timeout=TIMEOUT_SECONDS)


def _take_lock(folder):
    descriptor = _create_private(_file(folder, LOCK), os.O_RDWR | os.O_CREAT)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise QualityGateError(
            "another knowledge writer holds knowledge/writer.lock; nothing was written. "
            "Run the update again when it has finished."
        ) from None
    os.fchmod(descriptor, 0o600)
    return descriptor


@contextmanager
def writer(root):
    """The only way to change the index. Exit 1 at once if another writer is running."""
    require_fts5()
    folder = directory(root, create=True)
    lock = _take_lock(folder)
    try:
        path = _file(folder, INDEX)
        if not path.exists():
            os.close(_create_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        connection = _connect(path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            if not _has_schema(connection):
                _create_schema(connection)
            _meta(connection)
            yield Writer(connection)
        finally:
            connection.close()
    finally:
        os.close(lock)


class Reader:
    """One read transaction: every call sees the generation the reader started on."""

    def __init__(self, connection, meta):
        self._connection = connection
        self.generation = meta["generation"]

    def meta(self):
        return _meta(self._connection)

    def records(self, principal, ids=None):
        """The records `principal` may read, by id, optionally only those in `ids`."""
        clause, params = access_clause(principal)
        sql = f"SELECT body FROM records WHERE {clause}"
        if ids is not None:
            wanted = sorted(set(ids))
            if not wanted:
                return []
            sql += f" AND records.id IN ({_marks(wanted)})"
            params = [*params, *wanted]
        rows = self._connection.execute(sql + " ORDER BY records.id", params)
        return [json.loads(row[0]) for row in rows]

    def matching(self, principal, words):
        """(id, bm25) of the readable records sharing a term with `words`, best first. Each
        term is quoted as an FTS5 string and bound, so no FTS5 syntax comes from the input."""
        require_fts5()
        phrases = ['"' + word.replace('"', '""') + '"' for word in words if word]
        if not phrases:
            return []
        clause, params = access_clause(principal)
        rows = self._connection.execute(
            "SELECT record_terms.id, bm25(record_terms) FROM record_terms "
            "JOIN records ON records.id = record_terms.id "
            f"WHERE record_terms MATCH ? AND {clause} "
            "ORDER BY bm25(record_terms), record_terms.id",
            [" OR ".join(phrases), *params],
        )
        return [(row[0], row[1]) for row in rows]

    def provenance(self, principal):
        """{id: source} of the records `principal` may read."""
        clause, params = access_clause(principal)
        rows = self._connection.execute(
            f"SELECT records.id, records.source FROM records WHERE {clause} ORDER BY records.id",
            params,
        )
        return {row[0]: row[1] for row in rows}

    def edges(self, principal, relation):
        """(source id, target id) of the `relation` edges whose two ends `principal` may both
        read. An edge to or from a record the principal cannot read is not returned at all."""
        clause, params = access_clause(principal)
        readable = f"SELECT records.id FROM records WHERE {clause}"
        rows = self._connection.execute(
            "SELECT source_id, target_id FROM edges WHERE relation = ? "
            f"AND source_id IN ({readable}) AND target_id IN ({readable}) "
            "ORDER BY source_id, target_id",
            [relation, *params, *params],
        )
        return [(row[0], row[1]) for row in rows]

    def status_counts(self):
        """Operator view: how many records have each status. No record content."""
        rows = self._connection.execute(
            "SELECT status, COUNT(*) FROM records GROUP BY status ORDER BY status"
        )
        return {status: count for status, count in rows}

    def sources(self):
        rows = self._connection.execute(
            "SELECT name, adapter, cursor, state, last_success_at FROM sources ORDER BY name"
        )
        names = ("name", "adapter", "cursor", "state", "last_success_at")
        return [dict(zip(names, row)) for row in rows]

    def cursor(self, source):
        row = self._connection.execute(
            "SELECT cursor FROM sources WHERE name = ?", (source,)
        ).fetchone()
        return None if row is None else row[0]

    def tombstones(self):
        """Operator view: ids that left the index and why."""
        rows = self._connection.execute(
            "SELECT id, origin, source, reason, generation, at FROM tombstones ORDER BY id"
        )
        return [
            {
                "id": row[0],
                "origin": json.loads(row[1]),
                "source": row[2],
                "reason": row[3],
                "generation": row[4],
                "at": row[5],
            }
            for row in rows
        ]

    def plan(self, batch):
        """What a writer would do with `batch`, computed in this read transaction (dry run)."""
        return _plan(self._connection, batch).summary()


@contextmanager
def reader(root):
    path = _file(directory(root), INDEX)
    if not path.is_file():
        raise InputError("there is no knowledge index yet; run `sre-harness knowledge update`.")
    connection = sqlite3.connect(
        path.as_uri() + "?mode=ro", uri=True, isolation_level=None, timeout=TIMEOUT_SECONDS
    )
    try:
        connection.execute("BEGIN")
        meta = _meta(connection)  # the first read fixes the snapshot
        yield Reader(connection, meta)
    finally:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()


def plan(root, batch):
    """A dry run: what a writer would do, without the lock and without writing a byte. With no
    index yet, the plan is made against an empty one in memory."""
    require_fts5()
    if exists(root):
        with reader(root) as current:
            return current.plan(batch)
    return _empty_plan(batch)


def _empty_plan(batch):
    """What a writer would do with `batch` to an index that does not exist yet, in memory."""
    connection = sqlite3.connect(":memory:", isolation_level=None)
    try:
        _create_schema(connection)
        return _plan(connection, batch).summary()
    finally:
        connection.close()
