"""Edge cases of `spec analyze` on small synthetic repositories (real git, fixture transport)."""

import json
import subprocess
from types import SimpleNamespace

import pytest

from examples.synthetic.spec_repo import BASE_FILES, LOGS_TF, build, pull_item
from examples.synthetic.spec_scenarios import REQUIREMENTS, _http
from sre_harness import guard, records
from sre_harness.github import fixture as transport
from sre_harness.spec import prs as git_prs
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
                    "quote": "ログの保持期間を一覧で見られるようにする。",
                },
                {
                    "path": "docs/design/log-dashboard.md",
                    "to": "REQ-0001",
                    "relation": "refines",
                    "quote": "not in the document",
                },
                {
                    "path": "docs/absent.md",
                    "to": "REQ-0999",
                    "relation": "refines",
                    "quote": "どこにもない文書の引用",
                },
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


# Review fixes: line breaks, unreadable files, overlaps, shadowed values, caps, list gaps ----

TEST_365 = "def test_req_0001_keeps_365():\n    assert True\n"


def _conflicting(extra_one=None, extra_two=None, one_tf=None):
    """PR 301 sets REQ-0001 retention_days to 30 in IaC, PR 302 to 90 in the document."""
    return {
        301: _pull(
            "tf 30", {"infra/logs.tf": one_tf or _tf(30), **_test(301), **(extra_one or {})}
        ),
        302: _pull(
            "doc 90", {"docs/requirements.md": _requirements(90), **_test(302), **(extra_two or {})}
        ),
    }


@pytest.mark.parametrize("mark", ["\r", "\x0c", "\u2028", "\x85"])
def test_a_line_break_git_does_not_count_cannot_hide_a_semantic_conflict(tmp_path, home, mark):
    forged = f"note{mark}diff --git a/infra/logs.tf b/infra/logs.tf{mark}@@ -1,100 +1,1 @@\n"
    pulls = _conflicting(extra_two={"docs/design/notes.md": "# notes\n\n" + forged})
    _, _, found = analyzed(home, build(tmp_path, BASE_FILES, pulls))
    (conflict,) = by_kind(found, "semantic_conflict")
    assert conflict["prs"] == [301, 302]
    assert not any("overlap" in line for line in found["limitations"])


def test_a_form_feed_in_a_test_file_does_not_hide_the_added_test(tmp_path, home):
    tests = "import os\n# section\x0c\x0c\x0c\x0c\x0c\x0c\x0c\x0c\n" + (
        "def test_other():\n    pass\n" * 3
    )
    files = {**BASE_FILES, "tests/test_retention.py": tests}
    added = tests + "\n" + TEST_365
    pulls = {303: _pull("365", {"infra/logs.tf": _tf(365), "tests/test_retention.py": added})}
    _, _, found = analyzed(home, build(tmp_path, files, pulls))
    assert by_kind(found, "missing_test") == []


def test_an_overlap_in_an_unrelated_file_keeps_the_semantic_conflict(tmp_path, home):
    files = {**BASE_FILES, "CHANGELOG.md": "# changes\n- a\n"}
    pulls = _conflicting(
        extra_one={"CHANGELOG.md": "# changes\n- a\n- tf 30\n"},
        extra_two={"CHANGELOG.md": "# changes\n- a\n- doc 90\n"},
    )
    _, _, found = analyzed(home, build(tmp_path, files, pulls))
    (conflict,) = by_kind(found, "semantic_conflict")
    assert conflict["prs"] == [301, 302] and conflict["severity"] == "critical"
    assert "CHANGELOG.md" in conflict["explanation"]


def test_a_pr_file_that_is_not_utf8_is_input_missing_and_the_run_partial(tmp_path, home):
    one_tf = _tf(30).encode() + b"# caf\xe9\n"
    scenario = build(tmp_path, BASE_FILES, _conflicting(one_tf=one_tf))
    run_id, summary, found = analyzed(home, scenario, expect=1)
    assert found["status"] == "partial"
    (gap,) = [f for f in by_kind(found, "input_missing") if f["prs"] == [301]]
    assert gap["missing"] == "infra/logs.tf"
    (record,) = [pr for pr in found["prs"] if pr["number"] == 301]
    assert record["unread_files"] == ["infra/logs.tf"]


def test_a_value_shadowed_by_an_earlier_old_statement_is_still_a_change(tmp_path, home):
    shadow = 'resource "aws_s3_bucket" "aaa" {\n  retention_days = 400 # REQ-0001\n}\n\n' + _tf(30)
    scenario = build(tmp_path, BASE_FILES, _conflicting(one_tf=shadow))
    _, _, found = analyzed(home, scenario)
    (inside,) = [f for f in by_kind(found, "contradiction") if f["prs"]]
    assert inside["prs"] == [301] and inside["key"] == "retention_days"
    assert inside["entities"] == ["REQ-0001"]
    (conflict,) = by_kind(found, "semantic_conflict")
    assert conflict["prs"] == [301, 302]


def test_one_pr_over_the_diff_cap_is_input_missing_and_the_others_are_analyzed(
    tmp_path, home, monkeypatch
):
    pulls = {**_conflicting(), 304: _pull("big", {"infra/big.tf": "# x\n" * 2000})}
    scenario = build(tmp_path, BASE_FILES, pulls)
    monkeypatch.setattr(git_prs, "MAX_DIFF_BYTES", 4000)
    _, summary, found = analyzed(home, scenario, expect=1)
    assert found["status"] == "partial" and summary["unanalyzed"] == [304]
    (gap,) = [f for f in by_kind(found, "input_missing") if f["prs"] == [304]]
    assert "diff" in gap["explanation"]
    assert [f["prs"] for f in by_kind(found, "semantic_conflict")] == [[301, 302]]


def _rewrite_list(scenario, items):
    owner, name = "example-org", "demo"
    key = transport.rest_key(
        "pulls", {"owner": owner, "repo": name}, {"state": "open", "per_page": 100, "page": 1}
    )
    directory = scenario.fixture
    (directory / "s0001.http").write_text(_http(200, items), encoding="utf-8")
    (directory / transport.INDEX).write_text(json.dumps({"responses": {key: ["s0001.http"]}}))


def _items(scenario):
    return [
        pull_item(n, scenario.titles[n], scenario.heads[n], scenario.main_sha)
        for n in sorted(scenario.heads)
    ]


def test_list_items_without_a_number_are_input_missing_and_the_run_partial(tmp_path, home):
    scenario = build(tmp_path, BASE_FILES, {305: _pull("doc", {"docs/design/x.md": "# x\n"})})
    _rewrite_list(scenario, [*_items(scenario), "junk", {"title": "no number"}])
    _, _, found = analyzed(home, scenario, expect=1)
    assert found["status"] == "partial"
    gaps = [f for f in by_kind(found, "input_missing") if "PR list" in f["explanation"]]
    assert sorted(f["missing"] for f in gaps) == ["page 1 item 2", "page 1 item 3"]


def test_a_pr_based_on_another_branch_says_so(tmp_path, home):
    scenario = build(tmp_path, BASE_FILES, {306: _pull("tf 30", {"infra/logs.tf": _tf(30)})})
    items = _items(scenario)
    items[0]["base"]["ref"] = "feature/parent"
    _rewrite_list(scenario, items)
    _, _, found = analyzed(home, scenario)
    (record,) = found["prs"]
    assert record["base_is_default"] is False and record["base_ref"] == "feature/parent"
    assert any("PR #306" in line and "feature/parent" in line for line in found["limitations"])
    mine = [f for f in found["findings"] if 306 in f["prs"]]
    assert mine and all("not the default branch" in f["explanation"] for f in mine)


QUEUE_TF = 'resource "synthetic_queue" "jobs" {\n  visibility_seconds = 30 # DEC-0006\n}\n'


def test_written_ids_only_reach_entities_documents_define(tmp_path, home):
    files = {**BASE_FILES, "infra/queue.tf": QUEUE_TF}
    run_id, _, found = analyzed(home, build(tmp_path, files, {}), *MAIN)
    decisions = {
        d["external_id"]: d["id"]
        for d in read(home, run_id, "decisions.json")["decisions"]
        if "external_id" in d
    }
    assert decisions["ADR-0006"] == "DEC-0006"  # the harness's number, which no document writes
    assert "DEC-0006" in {f.get("missing") for f in by_kind(found, "input_missing")}


def test_a_finding_about_an_inferred_entity_is_inferred_and_capped(tmp_path, home):
    _, _, found = analyzed(home, build(tmp_path, BASE_FILES, {}), *MAIN, "--backend", "stub")
    about = [
        f for f in found["findings"] if any(e.startswith(("RQI-", "DCI-")) for e in f["entities"])
    ]
    assert about, "the stub infers RQI-0001 from the minutes; its ambiguity is a finding"
    for item in about:
        assert item["basis"] == "inferred" and item["inferred_entities"]
        assert item["severity"] in ("medium", "low", "info")


def test_a_linked_quote_shorter_than_eight_characters_is_dropped(tmp_path, home, monkeypatch):
    def answer(data):
        if data["conflicts"]:
            return {"links": [], "explanations": []}
        quote = "たら 5 分以内に"  # in the runbook; 9 characters, 7 besides the spaces
        return {
            "links": [
                {
                    "path": "docs/runbook/oncall.md",
                    "to": "REQ-0001",
                    "relation": "refines",
                    "quote": quote,
                }
            ],
            "explanations": [],
        }

    env = with_home(home, tmp_path)
    scenario = build(tmp_path, BASE_FILES, {})
    run_id = extracted(env, scenario)
    _claude_and_git(monkeypatch, answer)
    code, summary = analyze(env, run_id, scenario, *MAIN, "--backend", "claude", *LLM_ARGS)
    assert code == 0, summary
    reasons = [
        r for row in read(env, run_id, "analyze.json")["rejected_links"] for r in row["reasons"]
    ]
    assert [r.split(":")[0] for r in reasons] == ["quote_too_short"]
    assert records.violations(
        "llm-spec-analyze",
        {
            "links": [{"path": "a", "to": "REQ-0001", "relation": "refines", "quote": "ログ"}],
            "explanations": [],
        },
    )
