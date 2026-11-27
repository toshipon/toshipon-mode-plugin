"""container_state.py, the volume maintenance the image runs: backup under the writer lock,
restore into an empty root, import of the parts a host state root may lend, and the tar
members it refuses."""

import fcntl
import io
import os
import tarfile
from pathlib import Path

import pytest

import container_state
from tests.helpers import run_cli
from tests.knowledge_helpers import item, knowledge_dir, update, write_config, write_input


def tar_of(entries):
    """A tar from (name, bytes | None for a directory | TarInfo)."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for name, data in entries:
            if isinstance(data, tarfile.TarInfo):
                archive.addfile(data)
                continue
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.DIRTYPE
                archive.addfile(info)
            else:
                info.size = len(data)
                archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def run(command, root, data=b""):
    out = io.BytesIO()
    code = container_state.main([command, str(root)], stdin=io.BytesIO(data), stdout=out)
    return code, out.getvalue()


def names(data):
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        return archive.getnames()


@pytest.fixture
def indexed(home, tmp_path):
    """A state root with a config and an index of two records (generation 1)."""
    write_config(home)
    source = write_input(tmp_path / "in.json", [item("one"), item("two")])
    assert update(home, source)[0] == 0
    return home


def root_of(env):
    return knowledge_dir(env).parent


def generation(env):
    code, status = run_cli(["knowledge", "status"], env)
    assert code == 0
    return status["generation"], status["records"]


def test_a_backup_restores_into_an_index_of_the_same_generation(indexed, tmp_path):
    code, data = run("backup", root_of(indexed))
    assert code == 0
    listed = names(data)
    assert "knowledge/index.sqlite3" in listed and "knowledge/config.json" in listed
    assert not [name for name in listed if name.endswith(("-shm", "writer.lock"))]
    target = {"AI_HARNESS_HOME": str(tmp_path / "restored"), "PATH": "/usr/bin:/bin"}
    (tmp_path / "restored").mkdir()
    assert run("restore", tmp_path / "restored" / "sre-harness", data)[0] == 0
    assert generation(target) == generation(indexed)
    restored = tmp_path / "restored" / "sre-harness"
    assert restored.stat().st_mode & 0o777 == 0o700
    assert (restored / "knowledge" / "index.sqlite3").stat().st_mode & 0o777 == 0o600


def test_a_backup_waits_for_no_writer_it_stops_when_one_holds_the_lock(indexed):
    lock = knowledge_dir(indexed) / "writer.lock"
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, data = run("backup", root_of(indexed))
    finally:
        os.close(descriptor)
    assert (code, data) == (1, b"")


def test_a_backup_takes_the_lock_so_a_writer_meanwhile_stops(indexed, tmp_path, monkeypatch):
    seen = []
    real = container_state._members

    def members(root):
        source = write_input(tmp_path / "more.json", [item("one"), item("three")])
        seen.append(update(indexed, source)[0])
        yield from real(root)

    monkeypatch.setattr(container_state, "_members", members)
    assert run("backup", root_of(indexed))[0] == 0
    assert seen == [1]


def test_a_backup_of_no_state_is_exit_2(tmp_path):
    assert run("backup", tmp_path / "nothing")[0] == 2


def test_a_backup_refuses_a_symlink_in_the_state(indexed, tmp_path):
    (root_of(indexed) / "registry").mkdir()
    (root_of(indexed) / "registry" / "link").symlink_to(tmp_path)
    assert run("backup", root_of(indexed))[0] == 3


def test_a_restore_into_a_root_that_is_not_empty_is_refused(indexed):
    _, data = run("backup", root_of(indexed))
    assert run("restore", root_of(indexed), data)[0] == 3


@pytest.mark.parametrize(
    "member",
    ["../escape", "/absolute", ".", "knowledge/../../escape"],
)
def test_a_member_outside_the_root_stops_the_restore_and_leaves_nothing(tmp_path, member):
    data = tar_of([("knowledge/ok.json", b"{}"), (member, b"x")])
    root = tmp_path / "volume" / "sre-harness"
    root.parent.mkdir()
    assert run("restore", root, data)[0] == 3
    assert list(root.parent.iterdir()) == []


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_a_link_or_device_member_stops_the_restore(tmp_path, kind):
    link = tarfile.TarInfo("knowledge/link")
    link.type = kind
    link.linkname = "/etc/passwd"
    root = tmp_path / "volume" / "sre-harness"
    root.parent.mkdir()
    assert run("restore", root, tar_of([("knowledge", None), ("knowledge/link", link)]))[0] == 3
    assert list(root.parent.iterdir()) == []


def test_import_takes_the_config_registry_runs_and_git_cache(tmp_path):
    root = tmp_path / "volume" / "sre-harness"
    root.parent.mkdir()
    data = tar_of(
        [
            ("knowledge/config.json", b'{"a": 1}'),
            ("registry", None),
            ("registry/RG-0001/v0001.md", b"rule"),
            ("runs/run-x/documents.jsonl", b"{}\n"),
            ("cache/git/x.git/HEAD", b"ref"),
        ]
    )
    assert run("import", root, data)[0] == 0
    assert (root / "knowledge" / "config.json").read_bytes() == b'{"a": 1}'
    assert (root / "registry" / "RG-0001" / "v0001.md").stat().st_mode & 0o777 == 0o600
    assert (root / "cache" / "git" / "x.git").stat().st_mode & 0o777 == 0o700
    assert not [path.name for path in root.iterdir() if path.name.startswith(".import-")]
    # Again, with a new config: the file of the same name is replaced.
    assert run("import", root, tar_of([("knowledge/config.json", b'{"a": 2}')]))[0] == 0
    assert (root / "knowledge" / "config.json").read_bytes() == b'{"a": 2}'


@pytest.mark.parametrize(
    "member", ["knowledge/index.sqlite3", "knowledge/decisions/x.json", "cache/other", "x"]
)
def test_import_refuses_anything_else_and_imports_nothing(tmp_path, member):
    root = tmp_path / "volume" / "sre-harness"
    root.parent.mkdir()
    data = tar_of([("knowledge/config.json", b"{}"), (member, b"x")])
    assert run("import", root, data)[0] == 2
    assert not (root / "knowledge" / "config.json").exists()


def test_import_of_an_empty_tar_is_exit_2(tmp_path):
    assert run("import", tmp_path / "sre-harness", tar_of([]))[0] == 2


def test_import_stops_while_a_writer_holds_the_lock(indexed):
    lock = knowledge_dir(indexed) / "writer.lock"
    descriptor = os.open(lock, os.O_RDWR)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        code, _ = run("import", root_of(indexed), tar_of([("knowledge/config.json", b"{}")]))
    finally:
        os.close(descriptor)
    assert code == 1
    assert (knowledge_dir(indexed) / "config.json").read_bytes() != b"{}"


def test_import_refuses_to_write_through_a_symlink(tmp_path):
    root = tmp_path / "volume" / "sre-harness"
    root.mkdir(parents=True)
    (root / "registry").symlink_to(tmp_path)
    assert run("import", root, tar_of([("registry/RG-0001/v0001.md", b"x")]))[0] == 3


def test_import_checks_every_target_before_it_replaces_any(tmp_path):
    root = tmp_path / "volume" / "sre-harness"
    (root / "runs" / "zz").mkdir(parents=True)  # a directory where the tar has a file
    old = root / "registry" / "RG-0001"
    old.mkdir(parents=True)
    (old / "v0001.json").write_text("OLD json")
    data = tar_of(
        [
            ("registry/RG-0001/v0001.json", b"NEW json"),
            ("runs/zz", b"file"),
            ("registry/RG-0001/v0002.json", b"NEW"),
        ]
    )
    assert run("import", root, data)[0] == 2
    assert (old / "v0001.json").read_text() == "OLD json"
    assert not (old / "v0002.json").exists()
    assert sorted(p.name for p in root.iterdir()) == ["knowledge", "registry", "runs"]


def test_import_refuses_a_symlink_on_the_way_to_any_target_before_replacing(tmp_path):
    root = tmp_path / "volume" / "sre-harness"
    (root / "registry").mkdir(parents=True)
    (root / "runs").symlink_to(tmp_path)
    data = tar_of([("registry/RG-0001/v0001.md", b"x"), ("runs/r/documents.jsonl", b"{}")])
    assert run("import", root, data)[0] == 3
    assert not (root / "registry" / "RG-0001").exists()


def test_a_restored_index_gets_its_writer_lock_back(indexed, tmp_path):
    code, data = run("backup", root_of(indexed))
    assert code == 0
    restored = tmp_path / "restored" / "sre-harness"
    restored.parent.mkdir()
    assert run("restore", restored, data)[0] == 0
    lock = restored / "knowledge" / "writer.lock"
    assert lock.is_file() and lock.stat().st_size == 0
    assert lock.stat().st_mode & 0o777 == 0o600
    # A backup of the restored volume now holds the lock, as one of the original volume does.
    descriptor = container_state._lock(restored, create=False)
    assert descriptor is not None
    os.close(descriptor)


def test_a_tar_that_is_not_one_is_exit_2(tmp_path):
    root = tmp_path / "volume" / "sre-harness"
    root.parent.mkdir()
    assert run("restore", root, b"not a tar at all" * 64)[0] == 2


def test_the_tool_sits_beside_the_entry_point_the_image_copies():
    assert Path(container_state.__file__).parent == Path(__file__).resolve().parents[2]
