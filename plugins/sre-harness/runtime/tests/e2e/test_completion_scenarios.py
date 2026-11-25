"""The owner's completion conditions and the rest of design table 5.3, through cli.main.

One test (or a small group) per condition; README's table of conditions names these tests.
Every stage runs for real on synthetic data: GitHub is the fixture transport, git runs against
a local synthetic repository, and every model backend is the stub. That proves the mechanism
(collection, the holdout, the interpreter, labels, the registry, the trace, the conflict index,
SHA checks, the patch gate, the IaC contract). It says nothing about how good a model's output
would be, and the counts on synthetic data are not a claim of precision or recall.

Where a unit or e2e test already pins a detail, these tests reuse its helpers and check the
condition end to end instead of repeating every assertion.
"""

import json
import shutil

import pytest

from examples.synthetic import iac_scenarios as iac
from examples.synthetic import spec_repo
from examples.synthetic.github_responses import (
    BOT_ONLY_PRS,
    EXTERNAL_ONLY_PR,
    INJECTED_EXCEPTION,
    MISSING,
    OLD_POLICY_PR,
    TIMEOUT_FINDING,
    GitHubFixture,
    add_evaluation_prs,
    add_injected_exception,
    evaluation_scenario,
    review_learning_scenario,
)
from examples.synthetic.spec_repo import (
    ALL_PRS,
    CLEAN_PRS,
    DANGER_PATH,
    DANGER_PR,
    MISSING_TEST_PRS,
    PAIRS,
    SUPERSEDED_PR,
    advance_pull,
    build_parallel,
    build_patches,
)
from examples.synthetic.spec_scenarios import (
    ABSENT_FILE,
    HTTP_CLIENT_NEW,
    HTTP_CLIENT_OLD,
    INJECTED_ADR,
    MISSING_ADR,
    REJECTED_ADR,
    RETENTION_CHAIN,
    write_cycle_workspace,
    write_workspace,
)
from sre_harness import guard, records
from sre_harness.review import candidates
from sre_harness.review import cli as review_cli
from tests.helpers import (
    PERIOD,
    TOKEN_ENV,
    FakeClock,
    collect_fixture,
    evaluate_cli,
    generated_run,
    read_jsonl,
    run_cli,
    run_dir,
)
from tests.iac_helpers import no_opa, validate  # noqa: F401
from tests.iac_helpers import read as iac_read
from tests.spec_helpers import (
    analyzed,
    by_external,
    by_kind,
    counting_git,
    extract,
    ingest,
    ingested,
    read,
    report,
)

STUB = ["--backend", "stub"]
STREAM_EXCEPTION = "This is a stream, so no timeout is set on purpose."


def state(tmp_path_factory, name):
    root = tmp_path_factory.mktemp(name)
    return {"AI_HARNESS_HOME": str(root / "state"), "PATH": "/usr/bin:/bin"}, root


def copy_scenario(template, directory):
    """A private copy of a spec scenario, so a test may move refs."""
    shutil.copytree(template.source, directory / "source", symlinks=True)
    shutil.copytree(template.fixture, directory / "fixture")
    return spec_repo.Scenario(
        directory / "source",
        directory / "fixture",
        directory / "source" / "manifest.json",
        template.main_sha,
        dict(template.heads),
        dict(template.titles),
    )


def finding_prs(found):
    return {number for item in found["findings"] for number in item["prs"]}


# Review Learning: one evaluation run shared by the read-only checks ------------------------


@pytest.fixture(scope="module")
def review_eval(tmp_path_factory):
    """collect -> normalize -> split -> analyze -> classify -> generate -> evaluate, all stub.

    Generation side: six PRs raise and fix the missing timeout, one answers it with the stream
    exception. Evaluation side: PR 41 repeats the omission (flagged and fixed), PR 42 sets a
    timeout, PR 43 is the documented stream exception."""
    env, root = state(tmp_path_factory, "review-eval")
    with pytest.MonkeyPatch.context() as patch:
        clock = FakeClock()
        patch.setattr(review_cli, "clock", clock)
        patch.setattr(review_cli, "sleep", clock.sleep)
        run_id, split_path = generated_run(root, env, evaluation_scenario())
        code, summary = evaluate_cli(env, run_id, split_path)
    assert code == 0, summary
    directory = run_dir(env, run_id)
    evaluation = json.loads((directory / "evaluation.json").read_text())
    return env, run_id, directory, evaluation


def findings_by_pr(evaluation):
    found = {}
    for item in evaluation["findings"]:
        found.setdefault(item["pr"], []).append((item["rule_id"], item["label"]))
    return found


def test_condition_past_findings_are_reproduced_on_the_evaluation_hunk(review_eval):
    """完了条件: 過去指摘の再現（宣言的な候補が評価用 hunk で指摘し、recall の分子に入る）。"""
    _, run_id, directory, evaluation = review_eval
    assert records.violations("review-evaluation", evaluation) == []
    timeout = [
        candidates.load(directory, i)[0]
        for i in candidates.ids(directory)
        if candidates.load(directory, i)[0]["problem"] == TIMEOUT_FINDING
    ]
    (candidate,) = timeout
    assert candidate["form"] == "declarative_check"
    assert findings_by_pr(evaluation) == {41: [(candidate["id"], "human_flagged")]}
    with_candidates = evaluation["configs"]["with_candidates"]["counts"]
    assert with_candidates["true_positives"] == with_candidates["positives"] == 1
    # The mechanism, not a constant: the existing rules alone do not find it.
    assert evaluation["configs"]["baseline"]["counts"]["true_positives"] == 0
    assert evaluation["status"] == "unevaluable"  # 3 evaluation PRs: no metric value claimed


def test_condition_clean_review_hunks_draw_no_finding_and_keep_the_exception(review_eval):
    """完了条件: 正常例の誤検知（review。timeout 設定済みと例外を書いた hunk で指摘 0 件）。"""
    _, _, directory, evaluation = review_eval
    by_pr = findings_by_pr(evaluation)
    assert 42 not in by_pr and 43 not in by_pr
    assert evaluation["configs"]["with_candidates"]["counts"]["findings_on_clean"] == 0
    # In the same run the check does fire on PR 41, so silence on 42 and 43 is not a dead rule.
    assert 41 in by_pr
    assert {unit["pr"] for unit in evaluation["units"]} == {41, 42, 43}
    lessons = read_jsonl(directory / "lessons.jsonl")
    (lesson,) = [item for item in lessons if item["problem"] == TIMEOUT_FINDING]
    assert lesson["exceptions"] == [STREAM_EXCEPTION]


# Specification Consistency: the 25 open PRs, analyzed once --------------------------------


@pytest.fixture(scope="module")
def parallel_template(tmp_path_factory):
    return build_parallel(tmp_path_factory.mktemp("parallel"))


@pytest.fixture(scope="module")
def parallel_run(parallel_template, tmp_path_factory):
    """ingest -> extract (stub) -> analyze -> report over the 25 PRs, counting git processes."""
    env, root = state(tmp_path_factory, "parallel-run")
    scenario = copy_scenario(parallel_template, root / "copy")
    with pytest.MonkeyPatch.context() as patch:
        seen = counting_git(patch)
        run_id, summary, found = analyzed(env, scenario, *STUB)
    code, reported = report(env, run_id)
    assert code == 0, reported
    return env, run_id, summary, found, seen


def test_condition_the_16_clean_prs_get_no_spec_finding(parallel_run):
    """完了条件: 正常例の誤検知（spec。25 件のうち問題のない 16 件に指摘 0 件）。"""
    _, _, _, found, _ = parallel_run
    assert len(CLEAN_PRS) == 16
    assert finding_prs(found) & set(CLEAN_PRS) == set()
    analyzed_numbers = {pr["number"] for pr in found["prs"] if pr["analyzed_head_sha"]}
    assert set(CLEAN_PRS) <= analyzed_numbers  # analyzed, not skipped
    # In the same run every one of the 9 problem PRs does get a finding.
    assert set(ALL_PRS) - set(CLEAN_PRS) <= finding_prs(found)


def test_condition_25_parallel_prs_give_the_expected_findings_and_one_diff_per_pair(
    parallel_run,
):
    """完了条件: 25 件の並行 PR（semantic_conflict 3、superseded_dependency、missing_test 2、
    diff は (merge_base, head) ごとに 1 回）。"""
    env, run_id, summary, found, seen = parallel_run
    assert found["status"] == "complete" and len(found["prs"]) == len(ALL_PRS) == 25
    conflicts = by_kind(found, "semantic_conflict")
    assert sorted(item["prs"] for item in conflicts) == [list(pair) for pair in PAIRS]
    superseded = by_kind(found, "superseded_dependency", with_prs=True)
    assert [item["prs"] for item in superseded] == [[SUPERSEDED_PR]]
    missing = by_kind(found, "missing_test")
    assert sorted(item["prs"] for item in missing) == [[n] for n in MISSING_TEST_PRS]
    diffs = [argv for argv in seen if argv[1] == "diff" and "--name-status" not in argv]
    pairs = {(pr["merge_base"], pr["analyzed_head_sha"]) for pr in found["prs"]}
    assert len(pairs) == 24  # 124 and 125 share a head
    assert len(diffs) == len(pairs) == summary["diffs"]["computed"]
    assert summary["diffs"]["reused"] == 1
    value = read(env, run_id, "impact-report.json")
    assert records.violations("impact-report", value) == []


# Decision supersession -------------------------------------------------------------------


def test_condition_decision_supersession_chain_rejection_and_cycle(tmp_path, home, parallel_run):
    """完了条件: 決定の置換（置換、3 段の連鎖で末端だけ active、却下は置換できない、循環は exit 2）。"""
    run_id = ingested(home, write_workspace(tmp_path / "ws"))
    code, result = extract(home, run_id)
    assert code == 0, result
    decisions = read(home, run_id, "decisions.json")
    adr = by_external(decisions)
    old, new = adr[HTTP_CLIENT_OLD], adr[HTTP_CLIENT_NEW]
    assert old["status"] == "superseded" and old["superseded_by"] == [new["id"]]
    chain = [adr[name] for name in RETENTION_CHAIN]
    assert [item["status"] for item in chain] == ["superseded", "superseded", "approved"]
    active = set(decisions["active"])
    assert chain[-1]["id"] in active and not {item["id"] for item in chain[:-1]} & active
    # ADR-0007 is rejected and names ADR-0005 in `supersedes`: ADR-0005 stays active.
    assert adr[REJECTED_ADR]["status"] == "rejected"
    assert new["status"] == "approved" and new["superseded_by"] == [] and new["id"] in active

    # The document and the PR that still rely on the old decision get a finding.
    _, _, _, found, _ = parallel_run
    flagged = by_kind(found, "superseded_dependency")
    assert {tuple(item["prs"]) for item in flagged} == {(), (SUPERSEDED_PR,)}

    cycle = ingested(home, write_cycle_workspace(tmp_path / "cycle"))
    assert extract(home, cycle)[0] == 2
    assert not (run_dir(home, cycle) / "decisions.json").exists()


# Stale SHA -------------------------------------------------------------------------------


def test_condition_a_head_moved_after_analysis_is_stale_and_its_patch_is_refused(tmp_path, home):
    """完了条件: 古い SHA（head が進むと stale_sha、patch なし、exit 1。出力済み patch の
    check-patch は exit 3）。"""
    scenario = copy_scenario(build_patches(tmp_path / "template"), tmp_path / "copy")
    first, _, _ = analyzed(home, scenario)
    assert report(home, first)[0] == 0
    value = read(home, first, "impact-report.json")
    (pr_patch,) = [item for item in value["patches"] if item["target_pr"] == SUPERSEDED_PR]

    def check_patch(run_id, patch_id):
        argv = ["spec", "check-patch", "--run", run_id, "--patch", patch_id]
        argv += ["--fixture", str(scenario.fixture), "--git-source", str(scenario.source)]
        return run_cli(argv, home)

    assert check_patch(first, pr_patch["id"])[0] == 0  # the head has not moved yet

    advance_pull(scenario.source, SUPERSEDED_PR)  # the PR list still names the old head
    assert check_patch(first, pr_patch["id"])[0] == 3

    second, summary, found = analyzed(home, scenario, expect=1)
    assert summary["stale"] == [SUPERSEDED_PR] and found["status"] == "stale"
    assert [item["prs"] for item in by_kind(found, "stale_sha")] == [[SUPERSEDED_PR]]
    code, _ = report(home, second)
    assert code == 1
    again = read(home, second, "impact-report.json")
    assert [item for item in again["patches"] if item["target_pr"] == SUPERSEDED_PR] == []
    assert {item["target_pr"] for item in again["patches"]} == {None}  # main's fixes remain


# Missing input ---------------------------------------------------------------------------


def test_condition_missing_review_input_is_listed_not_filled(tmp_path, home, clock):
    """完了条件: 入力欠落（review。isResolved 欠落、ghost、途中ページの 404 で partial / exit 1、
    head.repo が null）。"""
    fixture = GitHubFixture(page_size=1)
    pull = fixture.pull(1, author=None, head_repo=None)  # a ghost author, a deleted fork
    thread = pull.thread(resolved=MISSING)
    for author in ("bob", None, "carol"):
        thread.comment(author, "Synthetic.")
    fixture.fail("pull_review_comments", page=2, number=1, status=404)
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 1 and summary["status"] == "partial" and summary["missing"] == 1
    code, result = run_cli(["review", "normalize", "--run", summary["run_id"]], home)
    assert code == 0 and result["collect_status"] == "partial"
    (inline,) = [
        item
        for item in read_jsonl(run_dir(home, summary["run_id"]) / "threads.jsonl")
        if item["kind"] == "inline_thread"
    ]
    assert {"thread.is_resolved", "pr.head_repo"} <= set(inline["missing"])
    assert "is_resolved" not in inline["thread"] and "head_repo" not in inline["pr"]
    assert inline["pr"]["author_type"] == "ghost"


def test_condition_missing_spec_input_is_input_missing(tmp_path, home, parallel_run):
    """完了条件: 入力欠落（spec。受入条件のない要求、存在しない決定への参照、manifest にある
    ファイルの不在、head.repo が null で fetch できない PR）。"""
    code, summary = ingest(home, write_workspace(tmp_path / "ws"))
    assert code == 1 and summary["status"] == "partial"
    run_id = summary["run_id"]
    assert extract(home, run_id)[0] == 0
    info = read(home, run_id, "extract.json")
    assert {
        "source_id": f"file:{ABSENT_FILE}",
        "locator": ABSENT_FILE,
        "reason": "file_not_found",
    } in info["input_missing"]
    assert {"kind": "decision_ref", "from": "REQ-0003", "ref": MISSING_ADR} in info["input_missing"]

    _, _, _, found, _ = parallel_run
    assert "REQ-0002" in {item["entities"][0] for item in by_kind(found, "ambiguous_requirement")}
    assert [item["missing"] for item in by_kind(found, "input_missing")] == [MISSING_ADR]

    scenario = build_parallel(tmp_path / "gone", numbers=ALL_PRS[:3], missing=(199,))
    _, summary, found = analyzed(home, scenario, expect=1)
    assert summary["unanalyzed"] == [199] and found["status"] == "partial"
    assert [item["prs"] for item in by_kind(found, "input_missing") if item["prs"]] == [[199]]


def test_condition_an_empty_period_is_unevaluable_not_a_number(tmp_path, home, clock):
    """完了条件: 入力欠落（空の期間。収集は 0 件で完了し、評価は unevaluable）。"""
    fixture = GitHubFixture()
    fixture.pull(1, updated_at="2025-06-01T00:00:00Z")
    code, summary = collect_fixture(fixture.write(tmp_path / "empty"), home)
    assert code == 0 and summary["status"] == "complete" and summary["pulls"] == 0

    # The evaluation side of a split with nothing after T1.
    run_id, split_path = generated_run(tmp_path, home, review_learning_scenario(), name="g")
    code, result = evaluate_cli(home, run_id, split_path)
    assert code == 0 and result["status"] == "unevaluable" and result["n_prs"] == 0
    code, output = evaluate_cli(home, run_id, split_path, "--format", "evaluator-output")
    assert code == 0 and output["metrics"] == {} and output["unevaluable"]


def test_condition_an_empty_auth_env_starts_nothing_and_exits_2(tmp_path, home, monkeypatch):
    """完了条件: 入力欠落（認証の環境変数が空なら通信せず exit 2。review と spec）。"""
    started = []
    monkeypatch.setattr(guard, "_run", lambda *args, **kwargs: started.append(args))
    env = {**home, TOKEN_ENV: ""}
    argv = ["review", "collect", "--repo", "example-org/demo", *PERIOD, "--auth-env", TOKEN_ENV]
    assert run_cli(argv, env) == (2, None)
    argv = ["spec", "analyze", "--run", "run-20260115T000000Z-0000abcd", "--prs", "open"]
    argv += ["--repo", "example-org/demo", "--auth-env", TOKEN_ENV]
    assert run_cli(argv, env)[0] == 2
    assert started == []


# The rest of design table 5.3 --------------------------------------------------------------


def test_scenario_an_old_policy_is_superseded_and_never_a_candidate(tmp_path, home, clock):
    """設計 5.3: 古い方針の分離（古い lesson は superseded、候補にならない）。"""
    run_id, _ = generated_run(tmp_path, home, evaluation_scenario(), untrusted=True)
    directory = run_dir(home, run_id)
    lessons = {row["id"]: row for row in read_jsonl(directory / "lessons.jsonl")}
    (old,) = [row for row in lessons.values() if OLD_POLICY_PR in row["prs"]]
    assert old["staleness"]["status"] == "superseded"
    aside = {row["lesson_id"]: row["reasons"] for row in read_jsonl(directory / "set-aside.jsonl")}
    assert "superseded" in aside[old["id"]]
    used = {
        lesson
        for i in candidates.ids(directory)
        for lesson in candidates.load(directory, i)[0]["lesson_ids"]
    }
    assert old["id"] not in used  # not even with --include-untrusted
    assert used  # other lessons did become candidates


def test_scenario_a_finding_only_outsiders_raised_is_set_aside(tmp_path, home, clock):
    """設計 5.3: 外部寄稿者だけの指摘（external_only、候補は生成されず別枠に出る）。"""
    run_id, _ = generated_run(tmp_path, home, evaluation_scenario())
    directory = run_dir(home, run_id)
    (external,) = [
        row for row in read_jsonl(directory / "lessons.jsonl") if row["prs"] == [EXTERNAL_ONLY_PR]
    ]
    assert external["source_trust"] == "external_only"
    aside = {row["lesson_id"]: row["reasons"] for row in read_jsonl(directory / "set-aside.jsonl")}
    assert "external_only" in aside[external["id"]]
    records_ = [candidates.load(directory, i)[0] for i in candidates.ids(directory)]
    assert records_ and all(external["id"] not in r["lesson_ids"] for r in records_)
    assert {r["source_trust"] for r in records_} == {"member_supported"}
    bot = [
        row for row in read_jsonl(directory / "lessons.jsonl") if row["prs"] == list(BOT_ONLY_PRS)
    ]
    assert [row["source_trust"] for row in bot] == ["bot_only"]


def test_scenario_rate_limits_are_waited_on_the_clock_and_a_cap_makes_partial(
    tmp_path, home, clock
):
    """設計 5.3: rate limit（残量 0 と secondary の 403 を注入した clock で待ち、上限超えは partial）。"""
    fixture = GitHubFixture()
    fixture.pull(1)
    fixture.limit("pulls", kind="exhausted", reset_in=120)
    fixture.limit("pull_review_comments", number=1, kind="secondary", retry_after=30)
    code, summary = collect_fixture(fixture.write(tmp_path / "waits"), home)
    assert code == 0 and summary["status"] == "complete"
    assert clock.slept == [120, 30] and summary["waited_seconds"] == 150

    capped = GitHubFixture()
    capped.pull(1)
    capped.limit("pull_reviews", number=1, kind="secondary", retry_after=30)
    code, summary = collect_fixture(capped.write(tmp_path / "cap"), home, "--max-wait", "10")
    assert code == 1 and summary["status"] == "partial" and summary["stopped"] == "wait_limit"


def test_scenario_duplicates_from_rest_graphql_and_page_overlap_are_one(tmp_path, home, clock):
    """設計 5.3: 重複（REST と GraphQL、ページ境界の重複は node_id で 1 件）。"""
    fixture = GitHubFixture(page_size=1, overlap=1)
    pull = fixture.pull(1)
    thread = pull.thread(path="src/client.py", line=12)
    asked = thread.comment("bob", "No timeout here.")
    thread.comment("carol", "Agreed.", reply_to=asked)
    thread.comment("alice", "Fixed.", reply_to=asked, association="OWNER")
    pull.issue_comment("carol", "Conversation.")
    pull.issue_comment("alice", "More conversation.")
    code, summary = collect_fixture(fixture.write(tmp_path / "fx"), home)
    assert code == 0
    assert run_cli(["review", "normalize", "--run", summary["run_id"]], home)[0] == 0
    threads = read_jsonl(run_dir(home, summary["run_id"]) / "threads.jsonl")
    (inline,) = [item for item in threads if item["kind"] == "inline_thread"]
    assert inline["node_id"] == thread.node_id
    assert len(inline["comments"]) == 3
    assert len({c["id"] for c in inline["comments"]}) == 3
    assert len([item for item in threads if item["kind"] == "issue_comment"]) == 2
    assert len({item["node_id"] for item in threads}) == len(threads)


def test_scenario_prompt_injection_changes_no_state_and_is_linted(tmp_path, home, clock):
    """設計 5.3: prompt injection（状態は変わらず、候補の lint に imperative_to_agent と url）。"""
    plain, _ = generated_run(tmp_path, home, evaluation_scenario(), name="plain", untrusted=True)
    # PR 11's body, a reply and an issue comment address an agent; an outsider's "exception"
    # tells it to approve the rule and open a URL.
    injected_fixture = review_learning_scenario(injection=True)
    add_injected_exception(injected_fixture)
    add_evaluation_prs(injected_fixture)
    injected, _ = generated_run(tmp_path, home, injected_fixture, name="inj", untrusted=True)

    def shape(run_id):
        directory = run_dir(home, run_id)
        loaded = [candidates.load(directory, i)[0] for i in candidates.ids(directory)]
        keys = ("status", "form", "target", "source_trust", "version", "problem")
        return sorted(json.dumps({k: r.get(k) for k in keys}, sort_keys=True) for r in loaded)

    assert shape(injected) == shape(plain)
    directory = run_dir(home, injected)
    (timeout,) = [
        record
        for record in (candidates.load(directory, i)[0] for i in candidates.ids(directory))
        if record["problem"] == TIMEOUT_FINDING
    ]
    assert {"imperative_to_agent", "url"} <= {item["kind"] for item in timeout["lint"]}
    assert INJECTED_EXCEPTION in timeout["exceptions"]
    # spec: the ADR that tells the reader to approve it stays proposed.
    run_id = ingested(home, write_workspace(tmp_path / "ws"))
    assert extract(home, run_id)[0] == 0
    assert by_external(read(home, run_id, "decisions.json"))[INJECTED_ADR]["status"] == "proposed"


def test_scenario_dangerous_patches_are_not_written_and_the_reason_is_kept(tmp_path, home):
    """設計 5.3: 危険な patch（.github/workflows/ と ../ は patch を出さず理由を記録）。"""
    scenario = copy_scenario(build_patches(tmp_path / "template"), tmp_path / "copy")
    run_id, _, found = analyzed(home, scenario)
    target = run_dir(home, run_id) / "findings.json"
    (pointer,) = [
        item
        for item in found["findings"]
        if item["prs"] == [SUPERSEDED_PR] and item["kind"] != "impact"
    ]
    pointer["evidence"][0]["ref"] = f"{scenario.heads[SUPERSEDED_PR]}:../outside.md#L3"
    target.write_text(json.dumps(found))
    assert report(home, run_id)[0] == 0
    value = read(home, run_id, "impact-report.json")
    refusals = {(item["path"], item["reason"]) for item in value["patch_rejections"]}
    assert (DANGER_PATH, "github_dir") in refusals
    assert ("../outside.md", "dotdot") in refusals
    assert all(DANGER_PR != item["target_pr"] for item in value["patches"])
    assert value["patches"]  # other fixes in the same run are still written
    written = run_dir(home, run_id) / "patches"
    for path in written.glob("*.diff"):
        text = path.read_bytes()
        assert DANGER_PATH.encode() not in text and b"../" not in text


def test_scenario_an_approver_who_implemented_the_change_is_a_violation(
    tmp_path,
    home,
    no_opa,  # noqa: F811
):
    """設計 5.3: IaC の遷移の不正（承認者 = 実装者は violation で exit 1）。"""
    code, summary = validate(home, iac.write_case(tmp_path / "valid"))
    assert code == 0 and summary["violations"] == {}
    code, summary = validate(home, iac.write_case(tmp_path / "bad", "approver_is_implementer"))
    assert code == 1 and summary["status"] == "violations"
    assert "approver_is_implementer" in summary["violations"]
    check = iac_read(home, summary["run_id"], "transition-check.json")
    assert "approver_is_implementer" in [item["code"] for item in check["violations"]]
