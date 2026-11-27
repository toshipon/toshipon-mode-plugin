"""Maintenance of the state volume of the container path: backup, restore and import.

It runs inside the image (`python -I container_state.py <command> <state root>`), with the
volume at /state and the state root at /state/sre-harness. It is not part of the harness and
is not on the harness's path; `scripts/sre-harness-container volume ...` starts it.

    backup  ROOT   a tar of the state root on stdout. The knowledge index is copied while the
                   writer lock (knowledge/writer.lock) is held, so no writer changes the index
                   file or its WAL meanwhile. The -shm, the lock and temporary files are left
                   out; SQLite rebuilds the -shm from the WAL when the index is opened again.
                   The volume can be mounted read-only: flock takes no write access.
    restore ROOT   a tar from stdin into a state root that is absent or empty. A restored
                   knowledge/ gets an empty writer.lock again (a backup leaves the lock out),
                   so that the next backup of that volume holds the lock as well.
    import  ROOT   a tar from stdin that holds only knowledge/config.json, registry/, runs/ and
                   cache/git/, as a host state root has them. Files of the same name are
                   replaced, under the writer lock. Every target is checked before any is
                   replaced (no symlink on its way, no directory where a file goes, no file
                   where a directory goes), so such a tar replaces nothing. The replacing is
                   one rename per file, not one for all: a failure in the middle of it (a full
                   disk) leaves the files renamed so far replaced and the rest as they were;
                   run the import again. The index is never imported: it is rebuilt with
                   `knowledge update`.

A tar member is extracted only when it is a regular file or a directory whose name is relative
and has no `..`; a link, a device or any other member stops the command with exit 3 and
nothing of the tar is left behind (it is unpacked into a staging directory beside the root
first). Files are written 0600, directories 0700, owned by whoever runs this.

Exit codes as the harness: 0 ok, 1 another writer holds the lock, 2 input, 3 refused.
"""

import argparse
import fcntl
import os
import secrets
import shutil
import sys
import tarfile
from pathlib import Path, PurePosixPath

EXIT_OK, EXIT_LOCKED, EXIT_INPUT, EXIT_REFUSED = 0, 1, 2, 3
LOCK = PurePosixPath("knowledge", "writer.lock")
SKIPPED_SUFFIXES = ("-shm",)
SKIPPED_PREFIXES = (".tmp-", ".staged-", ".restore-", ".import-")
IMPORTABLE_FILES = {PurePosixPath("knowledge", "config.json")}
IMPORTABLE_TREES = (PurePosixPath("registry"), PurePosixPath("runs"), PurePosixPath("cache", "git"))
IMPORTABLE_DIRECTORIES = {PurePosixPath("knowledge"), PurePosixPath("cache")}


class Stop(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _no_symlink(path):
    if path.is_symlink():
        raise Stop(EXIT_REFUSED, f"refused: {path} is a symlink.")


def _lock(root, create):
    """Hold knowledge/writer.lock (flock, without waiting). None when there is no lock file
    and `create` is False: no writer has ever run, so there is no index to protect."""
    path = root / LOCK
    _no_symlink(path.parent)
    _no_symlink(path)
    if not path.exists():
        if not create:
            return None
        path.parent.mkdir(mode=0o700, exist_ok=True)
    flags = (os.O_RDWR | os.O_CREAT) if create else os.O_RDONLY
    descriptor = os.open(path, flags | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        raise Stop(
            EXIT_LOCKED, "a knowledge writer holds knowledge/writer.lock; nothing was done."
        ) from None
    return descriptor


def _skipped(name):
    return name == LOCK.name or name.endswith(SKIPPED_SUFFIXES) or name.startswith(SKIPPED_PREFIXES)


def _members(root):
    """(relative path, absolute path) of what a backup holds, sorted, directories first."""
    for current, directories, files in os.walk(root):
        directories.sort()
        here = Path(current)
        for name in [*directories, *sorted(files)]:
            path = here / name
            if path.is_symlink():
                raise Stop(EXIT_REFUSED, f"refused to back up a symlink: {path}")
            if not _skipped(name):
                yield PurePosixPath(path.relative_to(root).as_posix()), path
        directories[:] = [name for name in directories if not _skipped(name)]


def backup(root, out):
    _no_symlink(root)
    if not root.is_dir():
        raise Stop(EXIT_INPUT, f"there is no state under {root} to back up.")
    lock = _lock(root, create=False)
    try:
        with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for name, path in _members(root):
                if path.is_dir():
                    info = archive.gettarinfo(str(path), arcname=str(name))
                    _anonymous(info)
                    archive.addfile(info)
                    continue
                # Opened first and described by fstat: a file replaced meanwhile (an atomic
                # write) is copied whole, as it was when it was opened.
                with open(path, "rb") as stream:
                    info = archive.gettarinfo(arcname=str(name), fileobj=stream)
                    _anonymous(info)
                    archive.addfile(info, stream)
    finally:
        if lock is not None:
            os.close(lock)
    return EXIT_OK


def _anonymous(info):
    info.uid = info.gid = 0
    info.uname = info.gname = ""


def _checked_name(member):
    name = PurePosixPath(member.name)
    if name.is_absolute() or not name.parts or ".." in name.parts:
        raise Stop(EXIT_REFUSED, f"refused tar member {member.name!r}: not a relative name.")
    if not (member.isfile() or member.isdir()):
        raise Stop(EXIT_REFUSED, f"refused tar member {member.name!r}: not a file or directory.")
    return name


def _importable(name, is_directory):
    if name in IMPORTABLE_FILES:
        return not is_directory
    if is_directory and name in IMPORTABLE_DIRECTORIES:
        return True
    return any(name == tree or tree in name.parents for tree in IMPORTABLE_TREES)


def _unpack(stream, staging, allowed=None):
    """Extract the tar on `stream` into `staging` (new). Returns the relative file names."""
    files = []
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            name = _checked_name(member)
            if allowed is not None and not allowed(name, member.isdir()):
                raise Stop(
                    EXIT_INPUT,
                    f"import takes only knowledge/config.json, registry/, runs/ and cache/git/; "
                    f"not {member.name!r}. Nothing was imported.",
                )
            target = staging.joinpath(*name.parts)
            _private_parents(staging, target.parent)
            if member.isdir():
                target.mkdir(mode=0o700, exist_ok=True)
                os.chmod(target, 0o700)
                continue
            source = archive.extractfile(member)
            descriptor = os.open(
                target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
            with os.fdopen(descriptor, "wb") as written:
                shutil.copyfileobj(source, written)
                written.flush()
                os.fsync(written.fileno())
            files.append(name)
    return files


def _private_parents(base, directory):
    missing = []
    while directory != base and not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for path in reversed(missing):
        path.mkdir(mode=0o700)
        os.chmod(path, 0o700)


def _staging(parent, prefix):
    path = parent / f"{prefix}{secrets.token_hex(4)}"
    path.mkdir(mode=0o700)
    return path


def _checked_target(root, name):
    """Where the file `name` goes, once nothing on its way stops it (exit 3 for a symlink,
    exit 2 for a directory or a file in the wrong place). Nothing is changed here."""
    target = root.joinpath(*name.parts)
    for parent in [*reversed(target.relative_to(root).parents)][1:]:
        path = root / parent
        _no_symlink(path)
        if path.exists() and not path.is_dir():
            raise Stop(EXIT_INPUT, f"refused to import {name}: {parent} is not a directory.")
    _no_symlink(target)
    if target.is_dir():
        raise Stop(EXIT_INPUT, f"refused to import {name}: a directory is there.")
    return target


def _lock_file(root):
    """An empty knowledge/writer.lock (0600) when knowledge/ is there without one."""
    folder = root / LOCK.parent
    if folder.is_dir() and not folder.is_symlink() and not (root / LOCK).exists():
        path = root / LOCK
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))


def restore(root, stream):
    _no_symlink(root)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise Stop(
            EXIT_REFUSED,
            f"refused to restore into {root}: it is not empty. Restore into a new volume.",
        )
    staging = _staging(root.parent, ".restore-")
    try:
        _unpack(stream, staging)
        if root.exists():
            root.rmdir()
        os.replace(staging, root)
        os.chmod(root, 0o700)
        _lock_file(root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return EXIT_OK


def import_state(root, stream):
    _no_symlink(root)
    _private_parents(root.parent, root)
    os.chmod(root, 0o700)
    lock = _lock(root, create=True)
    staging = _staging(root, ".import-")
    try:
        files = _unpack(stream, staging, allowed=_importable)
        if not files:
            raise Stop(EXIT_INPUT, "the tar holds no file to import; nothing was imported.")
        targets = [(name, _checked_target(root, name)) for name in files]  # all, first
        for name, target in targets:
            _private_parents(root, target.parent)
            os.replace(staging.joinpath(*name.parts), target)
        print(f"imported {len(files)} files into {root}", file=sys.stderr)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
        os.close(lock)
    return EXIT_OK


def main(argv=None, stdin=None, stdout=None):
    parser = argparse.ArgumentParser(prog="container_state", description=__doc__)
    parser.add_argument("command", choices=("backup", "restore", "import"))
    parser.add_argument("root", help="the state root (/state/sre-harness in the image)")
    args = parser.parse_args(argv)
    root = Path(args.root)
    stdin = stdin or sys.stdin.buffer
    stdout = stdout or sys.stdout.buffer
    try:
        if args.command == "backup":
            return backup(root, stdout)
        if args.command == "restore":
            return restore(root, stdin)
        return import_state(root, stdin)
    except Stop as stop:
        print(f"error: {stop}", file=sys.stderr)
        return stop.code
    except tarfile.TarError as error:
        print(f"error: not a readable tar: {error}", file=sys.stderr)
        return EXIT_INPUT
    except OSError as error:
        print(f"error: {error.strerror or error}: {error.filename}", file=sys.stderr)
        return EXIT_INPUT


if __name__ == "__main__":
    sys.exit(main())
