import os
import stat

import pytest

from sre_harness import paths
from sre_harness.errors import BoundaryError, InputError


def mode(path):
    return stat.S_IMODE(os.stat(path).st_mode)


def test_state_root_shares_the_improvement_harness_home(tmp_path):
    root = paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "state")})
    assert root == (tmp_path / "state" / "sre-harness").resolve()
    assert mode(root) == 0o700
    assert mode(root.parent) == 0o700


def test_state_root_falls_back_to_xdg_state_home(tmp_path):
    root = paths.state_root({"XDG_STATE_HOME": str(tmp_path)})
    assert root == (tmp_path / "ai-harness" / "sre-harness").resolve()


@pytest.mark.parametrize("where", ["repo/state", "repo/nested/deeper/state"])
def test_a_home_inside_a_git_repository_is_refused(tmp_path, where):
    (tmp_path / "repo" / ".git").mkdir(parents=True)
    with pytest.raises(BoundaryError, match="AI_HARNESS_HOME") as refused:
        paths.state_root({"AI_HARNESS_HOME": str(tmp_path / where)})
    assert refused.value.exit_code == 3
    assert not (tmp_path / where).exists()


def test_a_git_file_of_a_linked_worktree_also_counts(tmp_path):
    (tmp_path / "repo").mkdir()
    (tmp_path / "repo" / ".git").write_text("gitdir: elsewhere\n")
    with pytest.raises(BoundaryError):
        paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "repo" / "state")})


def test_a_symlinked_root_is_refused(tmp_path):
    (tmp_path / "real").mkdir()
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "sre-harness").symlink_to(tmp_path / "real")
    with pytest.raises(BoundaryError, match="symlink"):
        paths.state_root({"AI_HARNESS_HOME": str(tmp_path / "state")})


def test_a_run_directory_is_always_new(home):
    root = paths.state_root(home)
    run = paths.create_run(root)
    assert mode(run) == 0o700
    assert paths.list_runs(root) == [run.name]
    assert paths.open_run(root, run.name) == run
    with pytest.raises(InputError, match="already exists"):
        paths.create_run(root, run.name)


@pytest.mark.parametrize(
    "run_id", ["../x", "/etc", "run-1", "", None, "run-20260101T000000Z-zzzzzzzz"]
)
def test_a_run_is_named_by_id_never_by_path(home, run_id):
    with pytest.raises(InputError):
        paths.open_run(paths.state_root(home), run_id)


def test_files_are_written_private_and_whole(home):
    run = paths.create_run(paths.state_root(home))
    target = paths.write_private(run / "raw" / "page-0001.json", "{}")
    assert target.read_text() == "{}"
    assert mode(target) == 0o600
    assert mode(target.parent) == 0o700
    paths.write_private(target, "[]")
    assert target.read_text() == "[]"
    assert [entry.name for entry in target.parent.iterdir()] == ["page-0001.json"]


def test_writing_through_a_symlink_is_refused(home, tmp_path):
    run = paths.create_run(paths.state_root(home))
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    (run / "link").symlink_to(outside)
    with pytest.raises(BoundaryError):
        paths.write_private(run / "link", "overwritten")
    assert outside.read_text() == "keep"


@pytest.mark.parametrize("name", ["..", ".", "", "a/b", "a\\b", "a\0b"])
def test_output_names_cannot_leave_their_directory(tmp_path, name):
    with pytest.raises(BoundaryError):
        paths.child(tmp_path, "patches", name)


def test_output_names_from_ids_are_accepted(tmp_path):
    assert paths.child(tmp_path, "patches", "PT-0001.diff") == tmp_path / "patches" / "PT-0001.diff"
