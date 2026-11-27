import copy
import os
import stat

import pytest

from sre_harness import paths
from sre_harness.errors import BoundaryError, InputError
from sre_harness.knowledge import config
from tests.knowledge_helpers import CONFIG, knowledge_dir, write_config


def test_a_missing_config_loads_as_none(home):
    assert config.load(paths.state_root(home)) is None


def test_the_config_is_read_and_checked_against_its_schema(home):
    write_config(home)
    assert config.load(paths.state_root(home)) == CONFIG


@pytest.mark.parametrize(
    "change",
    [
        lambda value: value.pop("principals"),
        lambda value: value["principals"]["workspace-a"].update(classifications=["secret"]),
        lambda value: value["principals"]["workspace-a"].update(readers=[]),
        lambda value: value["principals"].update(
            {"bad name": copy.deepcopy(value["principals"]["workspace-b"])}
        ),
        lambda value: value.update(schema_version=2),
    ],
)
def test_a_config_that_breaks_the_schema_is_an_input_error(home, change):
    value = copy.deepcopy(CONFIG)
    change(value)
    write_config(home, value)
    with pytest.raises(InputError, match="knowledge-config"):
        config.load(paths.state_root(home))


def test_a_config_that_is_not_json_is_an_input_error(home):
    root = paths.state_root(home)
    paths.write_private(knowledge_dir(home) / "config.json", "{not json")
    with pytest.raises(InputError):
        config.load(root)


def test_a_symlinked_config_is_refused(home, tmp_path):
    root = paths.state_root(home)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    knowledge_dir(home).mkdir(mode=0o700)
    (knowledge_dir(home) / "config.json").symlink_to(target)
    with pytest.raises(BoundaryError, match="symlink"):
        config.load(root)


def test_a_symlinked_knowledge_directory_is_refused(home, tmp_path):
    root = paths.state_root(home)
    (tmp_path / "real").mkdir()
    knowledge_dir(home).symlink_to(tmp_path / "real")
    with pytest.raises(BoundaryError, match="symlink"):
        config.load(root)


def test_a_principal_is_resolved_by_name(home):
    write_config(home)
    principal = config.resolve(paths.state_root(home), "workspace-b")
    assert principal == config.Principal(
        name="workspace-b",
        readers=frozenset({"everyone", "team-alpha"}),
        classifications=frozenset({"public", "synthetic", "internal"}),
    )


def test_an_unknown_principal_is_refused_with_exit_3(home):
    write_config(home)
    with pytest.raises(BoundaryError) as refused:
        config.resolve(paths.state_root(home), "workspace-z")
    assert refused.value.exit_code == 3
    assert "workspace-a" not in str(refused.value)


def test_no_config_means_no_principal_and_exit_3(home):
    with pytest.raises(BoundaryError) as refused:
        config.resolve(paths.state_root(home), "workspace-a")
    assert refused.value.exit_code == 3


def test_the_known_readers_are_the_union_of_every_principal():
    assert config.known_readers(CONFIG) == {"everyone", "team-alpha", "team-gamma"}


def test_the_permissions_hash_follows_the_readers_and_classifications_only():
    a = config.principal(CONFIG, "workspace-a")
    b = config.principal(CONFIG, "workspace-b")
    assert config.permissions_hash(a) != config.permissions_hash(b)
    renamed = config.Principal("other", a.readers, a.classifications)
    assert config.permissions_hash(renamed) == config.permissions_hash(a)
    assert len(config.permissions_hash(a)) == 64


def test_the_config_hash_changes_with_any_change_and_not_with_key_order():
    reordered = {key: CONFIG[key] for key in reversed(list(CONFIG))}
    assert config.config_hash(reordered) == config.config_hash(CONFIG)
    changed = copy.deepcopy(CONFIG)
    changed["principals"]["workspace-b"]["readers"].append("team-gamma")
    assert config.config_hash(changed) != config.config_hash(CONFIG)


def test_the_config_stays_private(home):
    path = write_config(home)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
