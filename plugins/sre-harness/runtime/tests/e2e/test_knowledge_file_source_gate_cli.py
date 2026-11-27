"""`knowledge update --source file` is for synthetic data only. A file declares status,
approval, enforcement and access itself and passes no approval flow, so a state takes it in
only when `knowledge/config.json` says `"state_kind": "synthetic"`. Any other state (no
`state_kind`, or `"production"`) refuses it with exit 3 before the file is read."""

import pytest

from examples.synthetic import review_registry as rr
from sre_harness import paths
from sre_harness.knowledge import store
from tests.helpers import run_cli
from tests.knowledge_helpers import CONFIG, item, update, write_config, write_input

UNMARKED = {key: value for key, value in CONFIG.items() if key != "state_kind"}
SYNTHETIC = {**UNMARKED, "state_kind": "synthetic"}
PRODUCTION = {**UNMARKED, "state_kind": "production"}
SEARCH = ["knowledge", "search", "--principal", "workspace-a", "--project", "project-alpha"]


def mandatory(key):
    return item(key, enforcement="mandatory", summary=f"{key}: 合成の必須の規範")


@pytest.mark.parametrize("config", [UNMARKED, PRODUCTION], ids=["unmarked", "production"])
@pytest.mark.parametrize("extra", [(), ("--dry-run",)], ids=["update", "dry-run"])
def test_a_state_not_marked_synthetic_refuses_the_file_source(
    home, tmp_path, capsys, config, extra
):
    write_config(home, config)
    path = write_input(tmp_path / "in.json", [mandatory("R1")])
    code, result = update(home, path, *extra)
    assert code == 3 and result is None
    message = capsys.readouterr().err
    assert "--source file" in message and "state_kind" in message
    assert "review approve" in message
    assert not store.exists(paths.state_root(home))


def test_the_refusal_comes_before_the_input_is_read(home, tmp_path, capsys):
    write_config(home, PRODUCTION)
    code, _ = update(home, tmp_path / "there-is-no-such-file.json")
    assert code == 3
    assert "missing file" not in capsys.readouterr().err


def test_a_synthetic_state_takes_the_file_source_in(home, tmp_path):
    write_config(home, SYNTHETIC)
    path = write_input(tmp_path / "in.json", [mandatory("R1")])
    code, result = update(home, path)
    assert code == 0 and result["counts"]["inserted"] == 1
    code, found = run_cli(SEARCH, home)
    assert code == 0 and [row["enforcement"] for row in found["records"]] == ["mandatory"]


def test_a_production_state_still_takes_the_review_registry_in(home):
    made = rr.write(paths.state_root(home))
    write_config(home, {**made.config, "state_kind": "production"})
    code, result = run_cli(["knowledge", "update", "--source", "review-registry"], home)
    assert code == 0 and result["counts"]["indexed"] == 5


def test_a_synthetic_index_does_not_serve_once_the_state_is_marked_production(home, tmp_path):
    """Records a file put in stay stale under the production config, and the file source cannot
    be taken in again there: the index has to be rebuilt from the approval flow."""
    write_config(home, SYNTHETIC)
    path = write_input(tmp_path / "in.json", [mandatory("R1")])
    assert update(home, path)[0] == 0
    write_config(home, PRODUCTION)
    assert update(home, path)[0] == 3
    code, found = run_cli(SEARCH, home)
    assert code == 1 and found["status"] == "stale_index" and found["records"] == []
    assert found["stale_sources"] == ["file:synthetic"]
