"""Invisible and compatibility characters do not hide a secret shape or a denied term from the
checks, and do not reach the search output. Everything is synthetic."""

import json

import pytest

from sre_harness import privacy
from sre_harness.errors import BoundaryError, InputError
from sre_harness.knowledge import decisions, fetch, model
from tests.helpers import run_cli
from tests.knowledge_helpers import item, update, write_config, write_input

DENIED = ["project-secret"]
HIDDEN = {
    "zero width space (Cf)": "AKIA​ABCDEFGHIJKLMNOP",
    "combining grapheme joiner (Mn)": "AKIA͏ABCDEFGHIJKLMNOP",
    "variation selector (Mn)": "ghp_" + "️".join("A" * 36),
    "hangul filler": "sk-ant-ㅤ" + "a" * 25,
    "mongolian free variation selector": "AKIA᠋ABCDEFGHIJKLMNOP",
    "tag characters": "AKIA\U000e0041ABCDEFGHIJKLMNOP",
    "fullwidth": "ＡＫＩＡＡＢＣＤＥＦＧＨＩＪＫＬＭＮＯＰ",
}
DENY_HIDDEN = {
    "fullwidth": "ｐｒｏｊｅｃｔ-ｓｅｃｒｅｔ",
    "combining grapheme joiner": "project͏-secret",
    "variation selector": "project-se️cret",
}


def test_the_examined_form_drops_ignorable_combining_and_format_characters():
    for text in HIDDEN.values():
        assert privacy.find_secrets(privacy.examined(text)), text
    assert privacy.examined("a͏b​c️dㅤeﾠfᅟgᅠh") == "abcdefgh"
    assert privacy.examined("line one\nline\ttwo") == "line one\nline\ttwo"


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_fetch_withholds_a_secret_shape_behind_hidden_characters(name):
    assert fetch.withheld_shapes(f"a line: {HIDDEN[name]}\n", ()) != []


@pytest.mark.parametrize("name", sorted(DENY_HIDDEN))
def test_fetch_withholds_a_denied_term_behind_hidden_characters(name):
    assert fetch.DENY_TERM in fetch.withheld_shapes(f"a line: {DENY_HIDDEN[name]}\n", DENIED)


@pytest.fixture
def env(home, tmp_path):
    write_config(home)
    hostile = item(
        "B1",
        summary="rule ‏RLM ؜ALM ​ZWSP ⁠WJ end",
        applies_when=["x‎y"],
        exceptions=["‮reversed"],
    )
    assert update(home, write_input(tmp_path / "in.json", [item("R1"), hostile]))[0] == 0
    return home


def task_file(tmp_path):
    path = tmp_path / "task.json"
    value = {
        "schema_version": 1,
        "task_id": "TASK-0001",
        "workspace_id": "ws-a",
        "principal": "workspace-a",
        "project_id": "project-alpha",
        "component_id": None,
        "repo": "example-org/alpha",
        "base_sha": None,
        "changed_paths": [],
        "resource_types": [],
    }
    path.write_text(json.dumps(value))
    return path


def test_search_output_strings_lose_bidi_and_zero_width_characters(env):
    argv = ["knowledge", "search", "--principal", "workspace-a", "--project", "project-alpha"]
    code, result = run_cli(argv, env)
    assert code == 0
    for shown in result["records"]:
        for text in [shown["summary"], *shown["applies_when"], *shown["exceptions"]]:
            assert not any(c in text for c in "‏؜​⁠‎‮�"), text


@pytest.mark.parametrize("name", sorted(HIDDEN))
def test_a_question_with_a_hidden_secret_shape_is_not_put(env, tmp_path, name):
    argv = ["knowledge", "decision", "put", "--task-file", str(task_file(tmp_path))]
    argv += ["--question", f"token {HIDDEN[name]}", "--option", "yes", "--option", "no"]
    argv += ["--evidence", model.record_id("synthetic", "R1"), "--risk", "low"]
    assert run_cli(argv, env) == (2, None)


def test_a_reason_with_a_hidden_secret_shape_is_not_recorded(env):
    argv = ["knowledge", "search", "--principal", "workspace-a", "--project", "project-alpha"]
    argv += ["--component", "queue", "--task", "T-1", "--extend-task-budget", "10"]
    assert run_cli([*argv, "--reason", "追加の根拠が要る"], env)[0] == 0
    for name in ("combining grapheme joiner (Mn)", "variation selector (Mn)", "fullwidth"):
        assert run_cli([*argv, "--reason", "key " + HIDDEN[name]], env) == (2, None), name
    with pytest.raises(InputError):
        decisions.reason_text("key " + HIDDEN["combining grapheme joiner (Mn)"])


@pytest.mark.parametrize("name", ["Ｃｌａｕｄｅ", "Clau͏de", "review-ｂｏｔ", "Ｇ️ＰＴ4"])
def test_an_ai_name_behind_compatibility_or_combining_characters_is_refused(name):
    with pytest.raises(BoundaryError):
        decisions.require_person(name, env={})


@pytest.mark.parametrize("name", ["alice", "Claudette", "Ａｌｉｃｅ", "José"])
def test_a_person_s_name_still_answers(name):
    assert decisions.require_person(name, env={})
