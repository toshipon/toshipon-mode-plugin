import json

from sre_harness import cli


def test_runs_list_prints_the_root_and_the_runs(home, capsys):
    assert cli.main(["runs", "list"], env=home) == 0
    listing = json.loads(capsys.readouterr().out)
    assert listing["root"].endswith("/state/sre-harness")
    assert listing["runs"] == []


def test_a_wrong_command_line_is_an_input_error(capsys):
    assert cli.main(["runs", "delete-everything"]) == 2
    assert cli.main([]) == 2
    assert cli.main(["review", "push"]) == 2
    assert "error:" in capsys.readouterr().err


def test_a_refused_location_exits_3(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    assert cli.main(["runs", "list"], env={"AI_HARNESS_HOME": str(tmp_path / "state")}) == 3
    assert "AI_HARNESS_HOME" in capsys.readouterr().err


def test_version_is_not_an_error(capsys):
    assert cli.main(["--version"]) == 0


def test_the_review_group_has_collect_and_normalize(capsys):
    assert cli.main(["review", "collect", "--help"]) == 0
    assert "--auth-env" in capsys.readouterr().out
    assert cli.main(["review", "normalize", "--help"]) == 0
