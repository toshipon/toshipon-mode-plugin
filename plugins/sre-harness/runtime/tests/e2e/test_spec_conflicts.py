"""Edge cases of `spec analyze` on small synthetic repositories (real git, fixture transport)."""

import json
import subprocess
from types import SimpleNamespace

from examples.synthetic.spec_repo import BASE_FILES, LOGS_TF, build
from examples.synthetic.spec_scenarios import REQUIREMENTS
from sre_harness import guard
from tests.helpers import LLM_ARGS, envelope, prompt_parts, read_jsonl, run_dir, with_home
from tests.spec_helpers import analyze, analyzed, by_kind, extracted, read

OLD_LINE = "- retention_days: 400"
MAIN = ("--default-branch", "main")


def _requirements(value):
    return REQUIREMENTS.replace(OLD_LINE, f"- retention_days: {value}", 1)


def _tf(value):
    marker = "  retention_days = 400 # REQ-0001"
    return LOGS_TF.replace(marker, f"  retention_days = {value} # REQ-0001", 1)


def _test(number):
    return {f"tests/test_pr{number}.py": f"def test_req_0001_pr{number}():\n    assert True\n"}


def _pull(title, files):
    return {"title": title, "files": files}


def test_two_prs_that_set_the_same_value_do_not_conflict(tmp_path, home):
    scenario = build(
        tmp_path,
        BASE_FILES,
        {
            201: _pull("365 days", {"docs/requirements.md": _requirements(365), **_test(201)}),
            202: _pull("365 days too", {"infra/logs.tf": _tf(365), **_test(202)}),
        },
    )
    _, _, found = analyzed(home, scenario)
    assert by_kind(found, "semantic_conflict") == []
    assert by_kind(found, "missing_test") == []
    assert {tuple(item["prs"]) for item in by_kind(found, "impact")} == {(201,), (202,)}


def test_prs_whose_diffs_overlap_are_left_to_git_not_counted_as_semantic(tmp_path, home):
    scenario = build(
        tmp_path,
        BASE_FILES,
        {
            203: _pull("365", {"docs/requirements.md": _requirements(365), **_test(203)}),
            204: _pull("730", {"docs/requirements.md": _requirements(730), **_test(204)}),
            205: _pull("90 in IaC", {"infra/logs.tf": _tf(90), **_test(205)}),
        },
    )
    _, _, found = analyzed(home, scenario)
    (conflict,) = by_kind(found, "semantic_conflict")
    # 203 and 204 change the same line: a textual conflict. Each still disagrees with 205.
    assert conflict["prs"] == [203, 204, 205]
    assert any("PR #203 and PR #204" in line for line in found["limitations"])

    only_textual = build(
        tmp_path / "textual",
        BASE_FILES,
        {
            206: _pull("365", {"docs/requirements.md": _requirements(365), **_test(206)}),
            207: _pull("730", {"docs/requirements.md": _requirements(730), **_test(207)}),
        },
    )
    env = {**home, "AI_HARNESS_HOME": str(tmp_path / "state-textual")}
    _, _, found = analyzed(env, only_textual)
    assert by_kind(found, "semantic_conflict") == []
    assert any("PR #206 and PR #207" in line for line in found["limitations"])


def test_a_file_that_disagrees_with_the_registry_is_a_contradiction(tmp_path, home):
    files = {**BASE_FILES, "infra/logs.tf": _tf(90)}
    scenario = build(tmp_path, files, {})
    _, _, found = analyzed(home, scenario, *MAIN)
    (item,) = by_kind(found, "contradiction")
    assert item["entities"] == ["REQ-0001"] and item["key"] == "retention_days"
    assert item["severity"] == "high" and item["prs"] == []
    notes = [entry.get("note", "") for entry in item["evidence"]]
    assert "registry: retention_days: 400" in notes and "retention_days: 90" in notes


def test_without_open_prs_the_default_branch_must_be_named(tmp_path, home):
    scenario = build(tmp_path, BASE_FILES, {})
    code, _ = analyze(home, extracted(home, scenario), scenario)
    assert code == 2


def test_requirements_that_no_file_carries_are_not_reflected(tmp_path, home):
    scenario = build(tmp_path, BASE_FILES, {})
    _, _, found = analyzed(home, scenario, *MAIN)
    reflected = {item["entities"][0] for item in by_kind(found, "not_reflected")}
    assert "REQ-0005" in reflected  # only in the Confluence export, not in a repository file
    assert "REQ-0001" not in reflected


def test_an_inferred_link_alone_never_makes_a_high_finding(tmp_path, home):
    notes = "# HTTP の覚え書き\n\nHTTP クライアントは A を使う。\n"
    files = {**BASE_FILES, "docs/design/http-notes.md": notes}
    scenario = build(tmp_path, files, {})
    _, _, found = analyzed(home, scenario, *MAIN, "--backend", "stub")
    (item,) = [
        f
        for f in by_kind(found, "superseded_dependency")
        if any("http-notes" in e["ref"] or "llm-calls" in e["ref"] for e in f["evidence"])
    ]
    assert item["basis"] == "inferred" and item["severity"] == "medium"
    explicit = [f for f in by_kind(found, "superseded_dependency") if f["basis"] == "explicit"]
    assert explicit and all(f["severity"] == "high" for f in explicit)

    without = build(tmp_path / "none", files, {})
    env = {**home, "AI_HARNESS_HOME": str(tmp_path / "state-none")}
    _, _, found = analyzed(env, without, *MAIN)
    assert all(f["basis"] == "explicit" for f in found["findings"])


def test_a_pr_that_names_an_unknown_id_is_input_missing(tmp_path, home):
    doc = "# 新しい設計\n\nREQ-0042 に従う。\n"
    scenario = build(tmp_path, BASE_FILES, {208: _pull("doc", {"docs/design/new.md": doc})})
    _, _, found = analyzed(home, scenario)
    (item,) = [f for f in by_kind(found, "input_missing") if f["prs"]]
    assert item["prs"] == [208] and item["missing"] == "REQ-0042"


def test_a_deleted_test_or_document_is_read_from_the_base_side(tmp_path, home):
    scenario = build(
        tmp_path,
        BASE_FILES,
        {209: _pull("drop", {"docs/design/log-storage.md": None, "tests/test_http.py": None})},
    )
    _, _, found = analyzed(home, scenario)
    (impact,) = by_kind(found, "impact")
    assert "REQ-0001" in impact["entities"] and "REQ-0004" in impact["entities"]
    (missing,) = by_kind(found, "missing_test")  # the archive key went away with the document
    assert missing["entities"] == ["REQ-0001"]


def _claude_and_git(monkeypatch, answer):
    """Real git; claude answered from `answer(data)` without starting it."""
    calls = []

    def run(argv, **options):
        if argv[0].endswith("git"):
            return subprocess.run(argv, **options)
        data = prompt_parts(options["input"])[1]
        calls.append(data)
        stdout = json.dumps(envelope(answer(data)))
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="noise")

    real = guard._executable
    monkeypatch.setattr(guard, "_run", run)
    monkeypatch.setattr(
        guard, "_executable", lambda name: "/usr/bin/claude" if name == "claude" else real(name)
    )
    return calls


def test_claude_writes_explanations_and_its_bad_links_are_dropped(tmp_path, home, monkeypatch):
    scenario = build(
        tmp_path,
        BASE_FILES,
        {
            211: _pull("365", {"docs/requirements.md": _requirements(365), **_test(211)}),
            212: _pull("730", {"infra/logs.tf": _tf(730), **_test(212)}),
        },
    )

    def answer(data):
        if data["conflicts"]:
            return {
                "links": [],
                "explanations": [{"key": "C1", "text": "#211 は 365 日、#212 は 730 日です。"}],
            }
        return {
            "links": [
                {
                    "path": "docs/design/log-dashboard.md",
                    "to": "REQ-0001",
                    "relation": "refines",
                    "quote": "ログの保持期間",
                },
                {
                    "path": "docs/design/log-dashboard.md",
                    "to": "REQ-0001",
                    "relation": "refines",
                    "quote": "not in the document",
                },
                {"path": "docs/absent.md", "to": "REQ-0999", "relation": "refines", "quote": "x"},
            ],
            "explanations": [],
        }

    env = with_home(home, tmp_path)
    run_id = extracted(env, scenario)
    calls = _claude_and_git(monkeypatch, answer)
    code, summary = analyze(env, run_id, scenario, "--backend", "claude", *LLM_ARGS)
    assert code == 0, summary
    assert len(calls) == 2 and calls[1]["conflicts"][0]["entity"] == "REQ-0001"
    found = read(env, run_id, "findings.json")
    (conflict,) = by_kind(found, "semantic_conflict")
    assert conflict["explanation"].startswith("#211")
    assert conflict["explained_by"].startswith("LC-")
    assert conflict["severity"] == "critical"  # the model wrote words, not the verdict
    record = read(env, run_id, "analyze.json")
    reasons = [reason for row in record["rejected_links"] for reason in row["reasons"]]
    assert any(reason.startswith("quote_not_found") for reason in reasons)
    assert any(reason.startswith("unknown_entity") for reason in reasons)
    assert summary["links"]["inferred"] == 1
    rows = read_jsonl(run_dir(env, run_id) / "llm-calls.jsonl")
    assert [row["stage"] for row in rows] == ["extract", "analyze", "analyze"]
