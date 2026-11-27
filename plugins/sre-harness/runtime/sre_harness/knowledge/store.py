"""The knowledge index on disk, and the only module that speaks SQL.

    <state root>/knowledge/index.sqlite3   the index (WAL; rebuilt from the sources at will)
    <state root>/knowledge/writer.lock     held with flock by the one writer
    <state root>/knowledge/backups/        copies of the index taken before it was replaced

One writer at a time: `writer()` takes the lock without waiting, and a second writer stops with
exit 1 before it writes anything. One update is one `BEGIN IMMEDIATE` transaction; when it
changes anything, the generation goes up by one. A reader opens the file read-only and runs in
one read transaction, so it sees one generation from start to end, even while a writer is in
the middle of its transaction.

Which records a principal may read is decided in one place, `access_clause`. Every reader
method that returns records takes the principal first and puts that clause in its WHERE.

The whole index is replaced (migrate, rollback) only through `staged`, under the same lock: a
copy is built in a temporary file beside the index, and `Stage.commit` puts it in place with
one `os.replace`, so a crash leaves the old index or the new one. Before the rename, the WAL of
the index in place is checkpointed and emptied (TRUNCATE): its -wal and -shm stay at their
paths and the new file opens with them, so no frame of the old file may be left in them. A
reader that still reads frames from that WAL holds the checkpoint off; after the busy timeout
the replacement stops with exit 1 and changes nothing. A reader that has the old file open
keeps reading it (its own descriptor) until it ends: one command, one snapshot.
"""

import fcntl
import json
import os
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from .. import paths
from ..errors import BoundaryError, InputError, QualityGateError
from . import model, terms

DIRECTORY = "knowledge"
INDEX = "index.sqlite3"
LOCK = "writer.lock"
SCHEMA_VERSION = 2
SUPERSEDES = "supersedes"
CONFLICTS_WITH = "conflicts_with"
TIMEOUT_SECONDS = 5
BACKUPS = "backups"
# index-<UTC time>-g<generation>[-<n>].sqlite3; `n` tells apart two of one second and generation
BACKUP_NAME = re.compile(
    r"index-([0-9]{8}T[0-9]{6}Z)-g([0-9]{4,12})(?:-([2-9]|[1-9][0-9]{1,3}))?\.sqlite3"
)
MAX_BACKUPS_PER_NAME = 9999
STAGED = ".staged-"
SIDECARS = ("-wal", "-shm", "-journal")

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
        last_success_at TEXT NOT NULL, config_hash TEXT
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


def _probe_connection():
    return sqlite3.connect(":memory:")


def extension_loading():
    """Whether this Python's sqlite3 can load SQLite extensions: the connection has
    `enable_load_extension` and it can be switched on (and off again). Nothing is loaded. The
    index does not load any extension; `knowledge status` reports this for whoever wants one."""
    connection = _probe_connection()
    try:
        enable = getattr(connection, "enable_load_extension", None)
        if enable is None:
            return False
        enable(True)
        enable(False)
        return True
    except sqlite3.Error:
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
        "updated_at": rows.get("updated_at"),
    }


@contextmanager
def _intact(path):
    """A file SQLite cannot read as a database (not one, or damaged) is an input error with
    the way to rebuild it, not a traceback. A busy or locked database is not this."""
    try:
        yield
    except sqlite3.OperationalError:
        raise
    except sqlite3.DatabaseError as error:
        raise InputError(
            f"knowledge/{path.name} cannot be read as a knowledge index ({error}). The index "
            f"can be rebuilt: delete knowledge/{INDEX} and run `sre-harness knowledge update` "
            "again."
        ) from None


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
    foreign: list  # incoming ids another source holds: not written

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
            "owned_by_other_source": sorted(self.foreign),
            "tombstoned": [
                {"id": record_id, "reason": reason}
                for record_id, (_, reason) in sorted(self.deletes.items())
            ],
        }


def _plan(connection, batch):
    """What `batch` would change, read inside the caller's transaction. Refuses (exit 2) an
    update after which `supersedes` would have a cycle. A record whose id another source holds
    is not written (`foreign`): a source changes only its own records, and an id becomes free
    for another source once its owner has dropped it."""
    meta = _meta(connection)
    followed = connection.execute(
        "SELECT config_hash FROM sources WHERE name = ?", (batch.source,)
    ).fetchone()
    current = {
        row[0]: {"source": row[1], "hash": row[2], "origin": {"kind": row[3], "key": row[4]}}
        for row in connection.execute(
            "SELECT id, source, record_hash, origin_kind, origin_key FROM records"
        )
    }
    incoming = {record["id"]: record for record in batch.records}
    upserts, inserted, updated, unchanged, foreign = [], [], [], 0, []
    for record_id, record in sorted(incoming.items()):
        now = current.get(record_id)
        if now is None:
            inserted.append(record_id)
        elif now["source"] != batch.source:
            foreign.append(record_id)  # another source holds it: never overwritten from here
            continue
        elif now["hash"] != model.record_hash(record):
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
        config_changed=(followed[0] if followed else None) != batch.config_hash,
        upserts=upserts,
        inserted=inserted,
        updated=updated,
        unchanged=unchanged,
        deletes=deletes,
        foreign=foreign,
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
            [(record["id"], target, SUPERSEDES) for target in record["supersedes"]]
            + [
                (record["id"], row["target"], row["relation"])
                for row in record.get("relations", [])
            ],
        )
        connection.execute("DELETE FROM tombstones WHERE id = ?", (record["id"],))
    connection.executemany(
        "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
        [
            ("generation", str(generation)),
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
                "INSERT OR REPLACE INTO sources "
                "(name, adapter, cursor, state, last_success_at, config_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    batch.source,
                    batch.adapter,
                    batch.cursor,
                    "complete" if batch.complete else "partial",
                    now,
                    batch.config_hash,
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


class Locked:
    """The writer lock, held: what is read under it (a source, the index through `reader`)
    does not change under a writer, and `writer()` opens the index (made when there is none)."""

    def __init__(self, folder):
        self._folder = folder

    @contextmanager
    def writer(self):
        path = _file(self._folder, INDEX)
        if not path.exists():
            os.close(_create_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        connection = _connect(path)
        try:
            with _intact(path):
                connection.execute("PRAGMA journal_mode = WAL")
                if not _has_schema(connection):
                    _create_schema(connection)
                _meta(connection)
                yield Writer(connection)
        finally:
            connection.close()


@contextmanager
def locked(root):
    """The writer lock alone. Exit 1 at once if another writer is running."""
    require_fts5()
    folder = directory(root, create=True)
    lock = _take_lock(folder)
    try:
        yield Locked(folder)
    finally:
        os.close(lock)


@contextmanager
def writer(root):
    """The only way to change the index. Exit 1 at once if another writer is running."""
    with locked(root) as held, held.writer() as opened:
        yield opened


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
            "SELECT name, adapter, cursor, state, last_success_at, config_hash FROM sources "
            "ORDER BY name"
        )
        names = ("name", "adapter", "cursor", "state", "last_success_at", "config_hash")
        return [dict(zip(names, row)) for row in rows]

    def stale_sources(self, config_hash):
        """Names of the sources last taken in under another config than `config_hash`. Each
        source keeps the config it was last taken in with: while one of them is behind, who
        may read its records (and which readers it had rejected) may have changed under it."""
        rows = self._connection.execute(
            "SELECT name FROM sources WHERE config_hash IS NOT ? ORDER BY name", (config_hash,)
        )
        return [row[0] for row in rows]

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
        with _intact(path):
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


# Replacing the whole index -----------------------------------------------------------------


def _sidecar(path, suffix):
    return path.with_name(path.name + suffix)


def _remove(path):
    for suffix in ("", *SIDECARS):
        _sidecar(path, suffix).unlink(missing_ok=True)


def _fsync_directory(folder):
    descriptor = os.open(folder, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _open_quiet(path):
    """A read-only connection to an index file that writes nothing beside it, for a caller
    holding the writer lock (so no writer changes the file meanwhile). When the -wal holds
    frames they are part of the content and are read the usual way, through the -shm; else the
    file is opened immutable and no -wal or -shm is created."""
    wal = _sidecar(path, "-wal")
    held = wal.is_file() and wal.stat().st_size > 0
    query = "?mode=ro" if held else "?mode=ro&immutable=1"
    return sqlite3.connect(
        Path(path).absolute().as_uri() + query,
        uri=True,
        isolation_level=None,
        timeout=TIMEOUT_SECONDS,
    )


def _copy(source, target):
    """`target` (new, 0600): one consistent snapshot of the index file `source`, with what its
    WAL holds, through the SQLite backup API. It is left in rollback-journal mode, so that
    reading it later needs no -wal or -shm. Nothing of a half copy is left behind."""
    os.close(_create_private(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
    try:
        origin = _open_quiet(source)
        try:
            copy = _connect(target)
            try:
                origin.backup(copy)
                copy.execute("PRAGMA journal_mode = DELETE")
            finally:
                copy.close()
        finally:
            origin.close()
    except BaseException:
        _remove(target)
        raise


def _settle(index):
    """Fold the WAL of the index in place into its file and empty it (see the module doc)."""
    connection = _connect(index)
    try:
        busy, _, _ = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    finally:
        connection.close()
    wal = _sidecar(index, "-wal")
    if busy or (wal.is_file() and wal.stat().st_size > 0):
        raise QualityGateError(
            "a reader is still reading the current knowledge index from its WAL; nothing was "
            "replaced and nothing was backed up. Run it again when the reader has finished."
        )


def _backups_directory(folder, create=False):
    path = _file(folder, BACKUPS)
    if create and not path.exists():
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)
    return path


def _stamp(now):
    """2026-11-27T00:00:00Z -> 20261127T000000Z."""
    return now.replace("-", "").replace(":", "")


def _moment(stamp):
    """20261127T000000Z -> 2026-11-27T00:00:00Z."""
    return f"{stamp[:4]}-{stamp[4:6]}-{stamp[6:8]}T{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}Z"


def _backup(folder, index, now, generation):
    """Copy the index in place into backups/ under a new name. Returns {name, generation}."""
    base = _backups_directory(folder, create=True)
    for number in range(1, MAX_BACKUPS_PER_NAME + 1):
        suffix = "" if number == 1 else f"-{number}"
        name = f"index-{_stamp(now)}-g{generation:04d}{suffix}.sqlite3"
        path = paths.child(base, name)
        if path.exists() or path.is_symlink():
            continue
        _copy(index, path)
        return {"name": name, "generation": generation}
    raise QualityGateError(f"there are {MAX_BACKUPS_PER_NAME} backups of this second already.")


def backups(root):
    """The backups under knowledge/backups/, oldest first: {name, generation, created_at,
    bytes}. Only names the harness makes are listed."""
    base = _backups_directory(directory(root))
    if base.is_symlink() or not base.is_dir():
        return []
    found = []
    for path in base.iterdir():
        match = BACKUP_NAME.fullmatch(path.name)
        if match is None or path.is_symlink() or not path.is_file():
            continue
        found.append(
            (
                (match[1], int(match[2]), int(match[3] or 1)),
                {
                    "name": path.name,
                    "generation": int(match[2]),
                    "created_at": _moment(match[1]),
                    "bytes": path.stat().st_size,
                },
            )
        )
    return [entry for _, entry in sorted(found, key=lambda pair: pair[0])]


def backup_path(root, name):
    """The backup called `name`: only a name the harness makes (exit 2 otherwise), and one
    that exists (exit 2 when it does not)."""
    if not isinstance(name, str) or not BACKUP_NAME.fullmatch(name):
        raise InputError(
            f"not a backup name: {name!r}. `sre-harness knowledge rollback --list` shows them."
        )
    path = paths.child(_backups_directory(directory(root)), name)
    if path.is_symlink() or not path.is_file():
        raise InputError(
            f"there is no backup {name}. `sre-harness knowledge rollback --list` shows them."
        )
    return path


class Stage:
    """A whole new index, built in a temporary file beside the index in place (see `staged`)."""

    def __init__(self, folder, path, connection, current):
        self._folder = folder
        self._path = path
        self._connection = connection
        self._open = True
        self.current = current  # the generation of the index in place, None when there is none
        self.writer = Writer(connection)
        self.committed = False

    @contextmanager
    def read(self):
        """A Reader over the new index as it is now."""
        connection = self._connection
        connection.execute("BEGIN")
        try:
            yield Reader(connection, _meta(connection))
        finally:
            connection.execute("ROLLBACK")

    def advance(self, now):
        """Give the new index the next generation after both its own and the one in place, so
        that one generation never names two contents (a restored backup keeps its records,
        not its number). Returns the generation."""
        connection = self._connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            generation = max(_meta(connection)["generation"], self.current or 0) + 1
            connection.executemany(
                "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)",
                [("generation", str(generation)), ("updated_at", now)],
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        return generation

    def close(self):
        if self._open:
            self._connection.close()
            self._open = False

    def commit(self, now):
        """Back up the index in place, then put the new one in its place with one rename.
        Returns the backup ({name, generation}), or None when there was no index."""
        self.close()  # the only connection: closing it folds its WAL into the file
        if _sidecar(self._path, "-wal").exists():
            raise QualityGateError("the new knowledge index kept a WAL; nothing was replaced.")
        index = _file(self._folder, INDEX)
        saved = None
        if self.current is not None:
            _settle(index)
            saved = _backup(self._folder, index, now, self.current)
        os.replace(self._path, index)
        self.committed = True
        _fsync_directory(self._folder)
        return saved


def _generation_in_place(index):
    connection = _open_quiet(index)
    try:
        with _intact(index):
            return _meta(connection)["generation"]
    finally:
        connection.close()


def _open_stage(path, source):
    """A connection to the new file `path`: a copy of `source`, or an empty index without one,
    in WAL mode with the schema checked. A source that is no index of this schema: exit 2."""
    try:
        if source is None:
            os.close(_create_private(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL))
        else:
            _copy(source, path)
        connection = _connect(path)
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            if not _has_schema(connection):
                _create_schema(connection)
            _meta(connection)
        except BaseException:
            connection.close()
            raise
    except sqlite3.DatabaseError as error:
        raise InputError(
            f"{source.name if source else path.name} is no knowledge index: {error}."
        ) from None
    return connection


@contextmanager
def staged(root, start=None):
    """The only way to replace the whole index. Under the writer lock (exit 1 at once if
    another writer runs), a copy of the index in place, of the backup named `start`, or an
    empty index when there is neither, in a temporary file beside it. Change it through
    `.writer`, read it through `.read()`; `.commit(now)` puts it in place. Without a commit
    (a failed check, an exception) the temporary file is removed and the index is untouched."""
    require_fts5()
    source = None if start is None else backup_path(root, start)
    folder = directory(root, create=True)
    lock = _take_lock(folder)
    try:
        index = _file(folder, INDEX)
        current = _generation_in_place(index) if index.is_file() else None
        if source is None and current is not None:
            source = index
        path = paths.child(folder, f"{STAGED}{secrets.token_hex(8)}.sqlite3")
        stage = None
        try:
            stage = Stage(folder, path, _open_stage(path, source), current)
            yield stage
        finally:
            if stage is not None:
                stage.close()
            if stage is None or not stage.committed:
                _remove(path)
    finally:
        os.close(lock)


@contextmanager
def peek(root):
    """A Reader over the index in place that writes nothing to disk, for a dry run; None when
    there is no index. It holds the writer lock (exit 1 at once if a writer runs), so the file
    does not change while it is read."""
    folder = directory(root)
    index = _file(folder, INDEX)
    if not index.is_file():
        yield None
        return
    lock = _take_lock(folder)
    try:
        connection = _open_quiet(index)
        try:
            with _intact(index):
                connection.execute("BEGIN")
                yield Reader(connection, _meta(connection))
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
    finally:
        os.close(lock)


def plan_on(current, batch):
    """A dry run against the Reader `current`, or an empty index when it is None."""
    require_fts5()
    return _empty_plan(batch) if current is None else current.plan(batch)
