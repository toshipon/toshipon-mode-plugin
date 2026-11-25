"""ingest -> extract (stub) -> analyze -> report through the CLI, against a synthetic repository.

git runs for real (the harness's bare repository fetches from a local synthetic repository);
GitHub is the fixture transport and the backend is the stub. This proves the deterministic
parts (fetch, merge bases, the diff cache, the trace, the conflict index, the findings and the
report), not the quality of a model's explanations or inferred links.
"""

import json
import shutil
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from examples.synthetic import spec_repo
from examples.synthetic.spec_repo import (
    ALL_PRS,
    CLEAN_PRS,
    MARKDOWN_TITLE,
    MISSING_TEST_PRS,
    PAIRS,
    SAME_HEAD,
    SUPERSEDED_PR,
    advance_pull,
    build_parallel,
)
from sre_harness import records
from sre_harness.spec import findings as fd
from tests.helpers import run_dir
from tests.spec_helpers import analyze, analyzed, by_kind, counting_git, extracted, read, report

REPO_ROOT = Path(__file__).resolve().parents[5]
V2_SCHEMA = (
    REPO_ROOT
    / "plugins"
    / "toshipon-mode"
    / "skills"
    / "autonomous-improvement"
    / "schemas"
    / "evaluation.schema.json"
)


@pytest.fixture(scope="module")
def parallel_template(tmp_path_factory):
    return build_parallel(tmp_path_factory.mktemp("parallel"))


@pytest.fixture
def parallel(parallel_template, tmp_path):
    """A private copy of the 25-PR scenario, so a test may move refs."""
    copy = tmp_path / "copy"
    shutil.copytree(parallel_template.source, copy / "source", symlinks=True)
    shutil.copytree(parallel_template.fixture, copy / "fixture")
    scenario = spec_repo.Scenario(
        copy / "source",
        copy / "fixture",
        copy / "source" / "manifest.json",
        parallel_template.main_sha,
        dict(parallel_template.heads),
        dict(parallel_template.titles),
    )
    return scenario


def _diff_calls(argvs):
    return [argv for argv in argvs if argv[1] == "diff" and "--name-status" not in argv]


def test_25_parallel_prs_give_the_six_findings_and_nothing_for_the_clean_16(
    parallel, home, monkeypatch
):
    seen = counting_git(monkeypatch)
    run_id, summary, found = analyzed(home, parallel, "--backend", "stub")
    assert records.violations("impact-report", found, "findings_file") == []
    assert fd.check(found["findings"]) == []

    conflicts = by_kind(found, "semantic_conflict")
    assert sorted(item["prs"] for item in conflicts) == [list(pair) for pair in PAIRS]
    assert {item["key"] for item in conflicts} == {
        "retention_days",
        "archive_retention_days",
        "audit_retention_days",
    }
    assert all(item["entities"] == ["REQ-0001"] for item in conflicts)
    assert all(item["severity"] == "critical" and item["basis"] == "explicit" for item in conflicts)
    superseded = by_kind(found, "superseded_dependency", with_prs=True)
    assert [item["prs"] for item in superseded] == [[SUPERSEDED_PR]]
    missing = by_kind(found, "missing_test")
    assert sorted(item["prs"] for item in missing) == [[n] for n in MISSING_TEST_PRS]
    assert all(item["entities"] == ["REQ-0004"] for item in missing)

    clean = set(CLEAN_PRS)
    assert [item for item in found["findings"] if clean & set(item["prs"])] == []
    assert len(found["prs"]) == len(ALL_PRS) and found["status"] == "complete"
    assert summary["findings"]["semantic_conflict"] == 3

    # One diff per (merge base, head): 124 and 125 share a head, so 24 diffs for 25 PRs.
    pairs = {(pr["merge_base"], pr["analyzed_head_sha"]) for pr in found["prs"]}
    assert len(pairs) == len(ALL_PRS) - 1
    assert len(_diff_calls(seen)) == len(pairs) == summary["diffs"]["computed"]
    assert summary["diffs"]["reused"] == 1
    cached = sorted(path.name for path in (run_dir(home, run_id) / "diff-cache").iterdir())
    assert len(cached) == len(pairs) and all(name.endswith(".diff") for name in cached)


def test_the_diff_cache_is_reused_when_the_stage_runs_again(parallel, home, monkeypatch):
    run_id, _, first = analyzed(home, parallel)
    seen = counting_git(monkeypatch)
    code, summary = analyze(home, run_id, parallel)
    assert code == 0
    assert _diff_calls(seen) == []
    assert summary["diffs"] == {"computed": 0, "reused": len(ALL_PRS)}
    again = read(home, run_id, "findings.json")
    assert [item["kind"] for item in again["findings"]] == [
        item["kind"] for item in first["findings"]
    ]


def test_git_runs_grow_linearly_with_the_number_of_prs(tmp_path, home, monkeypatch):
    counts = {}
    for size in (5, 25):
        scenario = build_parallel(tmp_path / str(size), numbers=ALL_PRS[:size])
        env = {**home, "AI_HARNESS_HOME": str(tmp_path / f"state-{size}")}
        run_id = extracted(env, scenario)
        seen = counting_git(monkeypatch)
        code, summary = analyze(env, run_id, scenario)
        assert code == 0, summary
        assert summary["git_calls"] == len(seen)
        counts[size] = len(seen)
    per_pr = (counts[25] - counts[5]) / 20
    fixed = counts[5] - 5 * per_pr
    # merge-base, rev-parse, one diff, and two blob reads per readable changed file (at most
    # two per PR here): no more than 7 per PR, and nowhere near the 300 pairs of 25 PRs.
    assert per_pr <= 7
    assert counts[25] <= 7 * 25 + fixed
    assert counts[25] < 300


def test_a_head_that_moved_is_stale_and_the_others_are_still_analyzed(parallel, home):
    moved = PAIRS[0][0]
    new_head = advance_pull(parallel.source, moved)
    run_id, summary, found = analyzed(home, parallel, expect=1)
    assert summary["stale"] == [moved] and found["status"] == "stale"
    (record,) = [pr for pr in found["prs"] if pr["number"] == moved]
    assert record["stale"] and record["analyzed_head_sha"] == new_head
    assert record["head_sha"] == parallel.heads[moved]
    stale = by_kind(found, "stale_sha")
    assert [item["prs"] for item in stale] == [[moved]]
    assert [item for item in found["findings"] if moved in item["prs"] and item not in stale] == []
    # The other PRs are analyzed: the two other pairs still conflict.
    assert len(by_kind(found, "semantic_conflict")) == 2
    code, _ = report(home, run_id)
    assert code == 1


def test_a_pr_whose_head_cannot_be_fetched_is_input_missing(tmp_path, home):
    scenario = build_parallel(tmp_path / "s", numbers=ALL_PRS[:3], missing=(199,))
    _, summary, found = analyzed(home, scenario, expect=1)
    assert summary["unanalyzed"] == [199] and found["status"] == "partial"
    (gap,) = [item for item in by_kind(found, "input_missing") if item["prs"] == [199]]
    assert "deleted" in gap["evidence"][0]["note"]
    (record,) = [pr for pr in found["prs"] if pr["number"] == 199]
    assert record["analyzed_head_sha"] is None and record["merge_base"] is None
    assert any(item["prs"] == [101] for item in by_kind(found, "impact"))


def test_an_empty_auth_env_stops_before_anything_is_sent(parallel, home, monkeypatch):
    run_id = extracted(home, parallel)
    seen = counting_git(monkeypatch)
    env = {**home, "SPEC_READ_TOKEN": ""}
    argv = ["--run", run_id, "--prs", "open", "--repo", "example-org/demo"]
    from tests.helpers import run_cli

    code, _ = run_cli(["spec", "analyze", *argv, "--auth-env", "SPEC_READ_TOKEN"], env)
    assert code == 2 and seen == []
    assert not (run_dir(home, run_id) / "findings.json").exists()


def test_the_registry_gaps_come_through(parallel, home):
    _, _, found = analyzed(home, parallel)
    ambiguous = {item["entities"][0] for item in by_kind(found, "ambiguous_requirement")}
    assert "REQ-0002" in ambiguous  # no acceptance criteria
    missing = by_kind(found, "input_missing")
    assert [item["missing"] for item in missing] == ["ADR-0099"]


def test_supersession_flags_the_document_and_the_pr_that_rely_on_the_old_decision(parallel, home):
    _, _, found = analyzed(home, parallel)
    decisions = read_registry(home, found["run_id"])
    old = decisions["ADR-0002"]["id"]
    flagged = by_kind(found, "superseded_dependency")
    assert {tuple(item["prs"]) for item in flagged} == {(), (SUPERSEDED_PR,)}
    assert all(old in item["entities"] for item in flagged)
    (document,) = [item for item in flagged if not item["prs"]]
    assert "REQ-0004" in document["entities"]
    # ADR-0005 names ADR-0002 in `supersedes`: a successor is not a dependency.
    assert not any(decisions["ADR-0005"]["id"] in item["entities"] for item in flagged)


def read_registry(env, run_id):
    return {d.get("external_id"): d for d in read(env, run_id, "decisions.json")["decisions"]}


def test_the_trace_has_explicit_and_inferred_links_apart(parallel, home):
    run_id, summary, _ = analyzed(home, parallel, "--backend", "stub")
    links = read(home, run_id, "trace.json")
    assert records.violations("trace-link", links, "file") == []
    explicit = {
        (item["from"]["id"], item["to"]["id"], item["relation"])
        for item in links["links"]
        if item["basis"] == "explicit"
    }
    assert ("REQ-0004", read_registry(home, run_id)["ADR-0002"]["id"], "refines") in explicit
    assert ("aws_s3_bucket_lifecycle_configuration.app_logs", "REQ-0001", "implements") in explicit
    assert (
        "aws_s3_bucket_lifecycle_configuration.archive",
        "docs/design/log-storage.md",
        "implements",
    ) in explicit
    assert ("infra/logs.tf", "docs/design/log-storage.md", "implements") in explicit
    assert (
        "tests/test_http.py::test_req_0004_times_out",
        "REQ-0004",
        "verifies",
    ) in explicit
    inferred = [item for item in links["links"] if item["basis"] == "inferred"]
    assert ("docs/design/log-dashboard.md", "REQ-0001") in {
        (item["from"]["id"], item["to"]["id"]) for item in inferred
    }
    assert all(item["confidence"] == "low" for item in inferred)
    assert summary["links"]["inferred"] == len(inferred)


def test_report_writes_json_and_escaped_markdown(parallel, home):
    run_id, _, _ = analyzed(home, parallel, "--backend", "stub")
    code, summary = report(home, run_id)
    assert code == 0 and summary["findings"]["semantic_conflict"] == 3
    directory = run_dir(home, run_id)
    value = json.loads((directory / "impact-report.json").read_text())
    assert records.violations("impact-report", value) == []
    assert value["default_branch"]["moved_since_analysis"] is False
    assert {pr["number"] for pr in value["prs"]} == set(ALL_PRS)
    text = (directory / "impact-report.md").read_text()
    outside = _outside_code_blocks(text)
    assert MARKDOWN_TITLE not in outside
    for raw in ("](http", "![", "<b>", "`code`", "://"):
        assert raw not in outside
    for line in outside.splitlines():
        if line.startswith("| #110 "):
            assert line.count("|") - line.count("\\|") == 7  # six cells, no extra column
    assert "根拠（explicit）" in text and "根拠（inferred）" in text
    assert "docs/design/log-dashboard.md" in text  # the inferred link has its own column


def _outside_code_blocks(text):
    out, fence = [], None
    for line in text.splitlines():
        if fence is None and line.startswith("```"):
            fence = line.split("text")[0]
            continue
        if fence is not None:
            if line == fence:
                fence = None
            continue
        out.append(line)
    return "\n".join(out)


def test_evaluator_output_fits_the_v2_evaluator_schema(parallel, home):
    run_id, _, _ = analyzed(home, parallel)
    code, output = report(home, run_id, "--format", "evaluator-output")
    assert code == 0
    schema = json.loads(V2_SCHEMA.read_text())
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/output"})
    assert sorted(error.message for error in validator.iter_errors(output)) == []
    assert output["metrics"]["findings_semantic_conflict"] == 3
    assert output["metrics"]["findings_missing_test"] == 2
    assert output["metrics"]["prs_stale"] == 0
    assert len(output["evidence"]) == int(output["metrics"]["findings_total"])


def test_report_before_analyze_is_an_input_error(parallel, home):
    run_id = extracted(home, parallel)
    code, _ = report(home, run_id)
    assert code == 2


def test_analyze_before_extract_is_an_input_error(parallel, home):
    from tests.spec_helpers import ingested

    run_id = ingested(home, parallel.manifest)
    code, _ = analyze(home, run_id, parallel)
    assert code == 2


def test_the_same_head_pair_is_listed_and_diffed_once(parallel, home):
    _, _, found = analyzed(home, parallel)
    heads = {pr["number"]: pr["analyzed_head_sha"] for pr in found["prs"]}
    assert heads[SAME_HEAD[0]] == heads[SAME_HEAD[1]]


@pytest.mark.parametrize(
    ("extra", "code"),
    [
        (["--fixture", "FIXTURE"], 2),  # no --git-source
        (["--fixture", "FIXTURE", "--git-source", "relative/dir"], 2),
        (["--auth-env", "SPEC_READ_TOKEN", "--git-source", "SOURCE"], 2),
        (["--fixture", "FIXTURE", "--git-source", "SOURCE", "--repo", "../x"], 3),
        (["--fixture", "FIXTURE", "--git-source", "SOURCE", "--backend", "claude"], 2),
    ],
)
def test_analyze_refuses_unclear_sources_before_any_git(parallel, home, monkeypatch, extra, code):
    from tests.helpers import run_cli

    run_id = extracted(home, parallel)
    seen = counting_git(monkeypatch)
    values = {"FIXTURE": str(parallel.fixture), "SOURCE": str(parallel.source)}
    extra = [values.get(item, item) for item in extra]
    env = {**home, "SPEC_READ_TOKEN": "synthetic-value"}
    argv = ["spec", "analyze", "--run", run_id, "--prs", "open", "--repo", "example-org/demo"]
    result, _ = run_cli([*argv, *extra], env)
    assert result == code and seen == []
