"""Patches: generation in `spec report`, the temporary-index check, stale SHAs, `check-patch`.

git runs for real against the harness's bare repository; GitHub is the fixture transport and
the backend is the stub. The harness never applies a patch: the one test that applies them does
it itself, in a clone of the synthetic repository, to show the diffs are real.
"""

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from examples.synthetic import spec_repo
from examples.synthetic.spec_repo import (
    DANGER_PATH,
    DANGER_PR,
    NOTE_PATH,
    SUPERSEDED_PR,
    advance_branch,
    advance_pull,
    build_patches,
    run_git,
    write_pulls_fixture,
)
from sre_harness import records
from sre_harness.spec import patch
from sre_harness.spec import prs as git_prs
from tests.helpers import run_cli, run_dir
from tests.spec_helpers import analyzed, by_kind, counting_git, read, report

HTTP_RETRY = "docs/design/http-retry.md"


@pytest.fixture(scope="module")
def template(tmp_path_factory):
    return build_patches(tmp_path_factory.mktemp("patches"))


@pytest.fixture
def scenario(template, tmp_path):
    """A private copy, so a test may move refs."""
    copy = tmp_path / "copy"
    shutil.copytree(template.source, copy / "source", symlinks=True)
    shutil.copytree(template.fixture, copy / "fixture")
    return spec_repo.Scenario(
        copy / "source",
        copy / "fixture",
        copy / "source" / "manifest.json",
        template.main_sha,
        dict(template.heads),
        dict(template.titles),
    )


def reported(env, scenario, expect=0):
    run_id, _, found = analyzed(env, scenario, expect=expect)
    code, summary = report(env, run_id)
    assert code == expect, summary
    return run_id, read(env, run_id, "impact-report.json"), found


def check_patch(env, run_id, scenario, patch_id, *extra):
    argv = ["spec", "check-patch", "--run", run_id, "--patch", patch_id]
    argv += ["--fixture", str(scenario.fixture), "--git-source", str(scenario.source), *extra]
    return run_cli(argv, env)


def _by_path(value):
    return {path: item for item in value["patches"] for path in item["paths"]}


def test_report_writes_patches_for_document_fixes_and_records_every_refusal(scenario, home):
    run_id, value, found = reported(home, scenario)
    assert records.violations("impact-report", value) == []
    patches = _by_path(value)
    assert sorted(patches) == sorted(["docs/requirements.md", NOTE_PATH, HTTP_RETRY])

    # Default-branch fixes target the default branch's SHA; a PR's fix targets its head.
    assert patches["docs/requirements.md"]["target_pr"] is None
    assert patches["docs/requirements.md"]["target_head_sha"] == found["default_branch"]["sha"]
    assert patches[NOTE_PATH]["target_pr"] is None
    assert patches[HTTP_RETRY]["target_pr"] == SUPERSEDED_PR
    assert patches[HTTP_RETRY]["target_head_sha"] == scenario.heads[SUPERSEDED_PR]
    assert all(item["apply_check"] == "passed" for item in value["patches"])

    # Every patch points at its finding and back.
    findings = {item["id"]: item for item in value["findings"]}
    for item in value["patches"]:
        assert findings[item["finding_id"]]["patch_ref"] == item["id"]
        assert findings[item["finding_id"]]["kind"] in ("contradiction", "superseded_dependency")
    assert len({item["id"] for item in value["patches"]}) == len(value["patches"])
    refs = [item for item in value["findings"] if "patch_ref" in item]
    assert len(refs) == len(value["patches"])

    # The IaC side of the contradiction and the `.github/` fix are refused, with reasons.
    refusals = {(item["path"], item["reason"]) for item in value["patch_rejections"]}
    assert ("infra/logs.tf", "iac_or_code") in refusals
    assert (DANGER_PATH, "github_dir") in refusals
    (danger,) = [f for f in by_kind(value, "superseded_dependency") if f["prs"] == [DANGER_PR]]
    assert danger["prs"] == [DANGER_PR] and "patch_ref" not in danger

    directory = run_dir(home, run_id) / "patches"
    for item in value["patches"]:
        diff = (directory / f"{item['id']}.diff").read_bytes()
        sidecar = json.loads((directory / f"{item['id']}.json").read_text())
        assert records.violations("spec-patch", sidecar) == []
        assert sidecar["content_sha256"] == hashlib.sha256(diff).hexdigest()
        assert {k: sidecar[k] for k in item} == item
        assert DANGER_PATH.encode() not in diff and b"infra/" not in diff
    assert sorted(path.name for path in directory.iterdir()) == sorted(
        f"{item['id']}{suffix}" for item in value["patches"] for suffix in (".diff", ".json")
    )
    text = (run_dir(home, run_id) / "impact-report.md").read_text()
    assert "check-patch" in text and "github_dir" in text


def test_the_generated_patches_apply_and_fix_the_values(scenario, home, tmp_path):
    run_id, value, _ = reported(home, scenario)
    work = tmp_path / "work"
    run_git(tmp_path, "clone", "-q", str(scenario.source), str(work))
    run_git(work, "fetch", "-q", "origin", "+refs/pull/*:refs/pull/*")
    for item in value["patches"]:
        run_git(work, "checkout", "-q", "--detach", item["target_head_sha"])
        diff = run_dir(home, run_id) / "patches" / f"{item['id']}.diff"
        run_git(work, "apply", str(diff))
        for path in item["paths"]:
            text = (work / path).read_text()
            if path == NOTE_PATH:
                assert "- retention_days: 400" in text and "365" not in text
            else:
                assert "ADR-0005" in text and "ADR-0002" not in text
        run_git(work, "checkout", "-q", "--", ".")


def _snapshot(git_dir):
    """Every ref, HEAD and whether an index exists, read from the files themselves."""
    refs = {
        str(path.relative_to(git_dir)): path.read_text()
        for path in sorted((git_dir / "refs").rglob("*"))
        if path.is_file()
    }
    packed = git_dir / "packed-refs"
    return (
        refs,
        packed.read_text() if packed.exists() else None,
        (git_dir / "HEAD").read_text(),
        (git_dir / "index").exists(),
    )


def test_the_temporary_index_passes_the_target_fails_a_newer_commit_and_changes_nothing(
    scenario, home, tmp_path
):
    run_id, value, found = reported(home, scenario)
    item = _by_path(value)[HTTP_RETRY]
    data = (run_dir(home, run_id) / "patches" / f"{item['id']}.diff").read_bytes()
    root = Path(home["AI_HARNESS_HOME"]) / "sre-harness"
    repository = git_prs.Repository.existing(root, "example-org", "demo", str(scenario.source))
    allow = patch.Allow.from_run(run_dir(home, run_id))
    before = _snapshot(repository.git_dir)

    admitted = patch.admit(data, repository, item["target_head_sha"], allow, tmp_path / "w1")
    assert admitted["apply_check"] == "passed"
    assert _snapshot(repository.git_dir) == before
    assert admitted["preimage_blob_shas"] == item["preimage_blob_shas"]

    newer = advance_pull(
        scenario.source,
        SUPERSEDED_PR,
        {HTTP_RETRY: "# 再試行の設計\n\n外部呼び出しの再試行は別の方針に合わせる。\n"},
    )
    repository.fetch([f"+refs/pull/{SUPERSEDED_PR}/head:{git_prs.pull_ref(SUPERSEDED_PR)}"])
    after_fetch = _snapshot(repository.git_dir)
    assert patch.admit(data, repository, newer, allow, tmp_path / "w2")["apply_check"] == "failed"
    assert _snapshot(repository.git_dir) == after_fetch
    assert before[1:] == after_fetch[1:]  # HEAD, packed refs and the absent index stay
    assert not (repository.git_dir / "index").exists()


def test_a_pr_whose_head_moved_before_the_analysis_gets_stale_sha_and_no_patch(scenario, home):
    advance_pull(scenario.source, SUPERSEDED_PR)
    run_id, value, found = reported(home, scenario, expect=1)
    assert found["status"] == "stale"
    stale = by_kind(value, "stale_sha")
    assert [item["prs"] for item in stale] == [[SUPERSEDED_PR]]
    assert [p for p in value["patches"] if p["target_pr"] == SUPERSEDED_PR] == []
    assert not any(
        SUPERSEDED_PR in item["prs"] and "patch_ref" in item for item in value["findings"]
    )
    # The default branch's fixes are still offered.
    assert {p["target_pr"] for p in value["patches"]} == {None}
    assert records.violations("impact-report", value) == []


def test_a_patch_on_a_pr_finding_marked_stale_is_refused(scenario, home):
    run_id, _, _ = analyzed(home, scenario)
    target = run_dir(home, run_id) / "findings.json"
    found = json.loads(target.read_text())
    for pr in found["prs"]:
        if pr["number"] == SUPERSEDED_PR:
            pr["stale"] = True
    target.write_text(json.dumps(found))
    report(home, run_id)
    value = read(home, run_id, "impact-report.json")
    assert [p for p in value["patches"] if p["target_pr"] == SUPERSEDED_PR] == []
    assert any(
        item["reason"] == "stale_sha" and item["target_pr"] == SUPERSEDED_PR
        for item in value["patch_rejections"]
    )


def test_a_finding_that_points_outside_the_repository_gets_no_patch(scenario, home):
    run_id, _, _ = analyzed(home, scenario)
    target = run_dir(home, run_id) / "findings.json"
    found = json.loads(target.read_text())
    (item,) = [
        f for f in found["findings"] if f["prs"] == [SUPERSEDED_PR] and f["kind"] != "impact"
    ]
    sha = scenario.heads[SUPERSEDED_PR]
    item["evidence"][0]["ref"] = f"{sha}:../outside.md#L3"
    target.write_text(json.dumps(found))
    code, _ = report(home, run_id)
    assert code == 0
    value = read(home, run_id, "impact-report.json")
    assert [p for p in value["patches"] if p["target_pr"] == SUPERSEDED_PR] == []
    (refusal,) = [r for r in value["patch_rejections"] if r["finding_id"] == item["id"]]
    assert refusal["reason"] == "dotdot" and refusal["path"] == "../outside.md"


# check-patch ------------------------------------------------------------------------------


def test_e2e_check_patch_passes_while_the_heads_have_not_moved(scenario, home):
    run_id, value, _ = reported(home, scenario)
    for item in value["patches"]:
        code, out = check_patch(home, run_id, scenario, item["id"])
        assert code == 0, out
        assert out["apply_check"] == "passed" and out["current_head_sha"] == item["target_head_sha"]
        assert out["patch"] == item["id"] and out["applied"] is False


def test_check_patch_refuses_a_pr_patch_after_the_head_moved(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)[HTTP_RETRY]
    new = advance_pull(scenario.source, SUPERSEDED_PR)
    # The list still names the old head, the fetched ref is the new one.
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3
    # The list and the ref both name the new head.
    scenario.heads[SUPERSEDED_PR] = new
    write_pulls_fixture(scenario)
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_check_patch_refuses_a_default_branch_patch_after_the_branch_moved(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)["docs/requirements.md"]
    advance_branch(scenario.source, {"docs/runbook/later.md": "# later\n"})
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_check_patch_refuses_a_rewritten_patch_file(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)[HTTP_RETRY]
    diff = run_dir(home, run_id) / "patches" / f"{item['id']}.diff"
    diff.write_bytes(diff.read_bytes().replace(b"ADR-0005", b"ADR-0009"))
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_check_patch_refuses_a_rewritten_sidecar(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)[HTTP_RETRY]
    sidecar = run_dir(home, run_id) / "patches" / f"{item['id']}.json"
    data = json.loads(sidecar.read_text())
    data["paths"] = [DANGER_PATH]
    sidecar.write_text(json.dumps(data))
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def _rewrite(path, change):
    data = json.loads(path.read_text())
    change(data)
    path.write_text(json.dumps(data))


def test_check_patch_refuses_an_old_patch_whose_sidecar_names_the_new_head(scenario, home):
    """main moves (an unrelated file); only the sidecar's `target_head_sha` is rewritten.
    `impact-report.json` still names the old head, so the patch is not offered."""
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)["docs/requirements.md"]
    new = advance_branch(scenario.source, {"docs/runbook/later.md": "# later\n"})
    sidecar = run_dir(home, run_id) / "patches" / f"{item['id']}.json"
    _rewrite(sidecar, lambda data: data.update(target_head_sha=new))
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


@pytest.mark.parametrize(
    "change",
    [
        lambda entry: entry.update(content_sha256="0" * 64),
        lambda entry: entry.update(paths=[DANGER_PATH]),
        lambda entry: entry.update(target_pr=SUPERSEDED_PR),
        lambda entry: entry.update(preimage_blob_shas={"docs/requirements.md": "0" * 40}),
    ],
    ids=["content_sha256", "paths", "target_pr", "preimage_blob_shas"],
)
def test_check_patch_refuses_a_sidecar_that_differs_from_the_report(scenario, home, change):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)["docs/requirements.md"]

    def edit(report_value):
        change(next(p for p in report_value["patches"] if p["id"] == item["id"]))

    _rewrite(run_dir(home, run_id) / "impact-report.json", edit)
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_check_patch_refuses_a_patch_the_report_does_not_list(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)["docs/requirements.md"]

    def drop(report_value):
        report_value["patches"] = [p for p in report_value["patches"] if p["id"] != item["id"]]

    _rewrite(run_dir(home, run_id) / "impact-report.json", drop)
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_check_patch_refuses_both_records_rewritten_when_the_target_file_changed(scenario, home):
    """The sidecar and the report both name the new head, but the patched file changed on
    main: its blob is not the preimage the patch was made for."""
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)["docs/requirements.md"]
    before = run_git(scenario.source, "show", f"{item['target_head_sha']}:docs/requirements.md")
    new = advance_branch(scenario.source, {"docs/requirements.md": before + "\n追記です。\n"})

    def to_new(data):
        entries = data["patches"] if "patches" in data else [data]
        for entry in entries:
            if entry["id"] == item["id"]:
                entry["target_head_sha"] = new

    _rewrite(run_dir(home, run_id) / "patches" / f"{item['id']}.json", to_new)
    _rewrite(run_dir(home, run_id) / "impact-report.json", to_new)
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


@pytest.mark.parametrize(("name", "code"), [("PT-9999", 2), ("../x", 3), ("PT-0001.diff", 3)])
def test_check_patch_names_must_be_patch_ids_of_the_run(scenario, home, monkeypatch, name, code):
    run_id, _, _ = reported(home, scenario)
    seen = counting_git(monkeypatch)
    result, _ = check_patch(home, run_id, scenario, name)
    assert result == code and seen == []


def test_check_patch_with_an_empty_auth_env_starts_nothing(scenario, home, monkeypatch):
    run_id, value, _ = reported(home, scenario)
    seen = counting_git(monkeypatch)
    env = {**home, "SPEC_READ_TOKEN": ""}
    argv = ["spec", "check-patch", "--run", run_id, "--patch", value["patches"][0]["id"]]
    code, _ = run_cli([*argv, "--auth-env", "SPEC_READ_TOKEN"], env)
    assert code == 2 and seen == []


def test_check_patch_needs_a_source(scenario, home):
    run_id, value, _ = reported(home, scenario)
    argv = ["spec", "check-patch", "--run", run_id, "--patch", value["patches"][0]["id"]]
    code, _ = run_cli([*argv, "--fixture", str(scenario.fixture)], home)
    assert code == 2


def test_check_patch_never_writes_to_the_source_repository(scenario, home):
    run_id, value, _ = reported(home, scenario)
    before = run_git(scenario.source, "for-each-ref")
    status = run_git(scenario.source, "status", "--porcelain")
    for item in value["patches"]:
        code, _ = check_patch(home, run_id, scenario, item["id"])
        assert code == 0
    after = run_git(scenario.source, "for-each-ref")
    assert before == after and run_git(scenario.source, "status", "--porcelain") == status


def test_check_patch_refuses_a_patch_for_a_pr_that_is_no_longer_open(scenario, home):
    run_id, value, _ = reported(home, scenario)
    item = _by_path(value)[HTTP_RETRY]
    del scenario.heads[SUPERSEDED_PR]
    write_pulls_fixture(scenario)
    code, _ = check_patch(home, run_id, scenario, item["id"])
    assert code == 3


def test_a_diff_for_a_symlink_in_the_tree_is_refused_by_mode(scenario, home, tmp_path):
    run_id, _, _ = reported(home, scenario)
    link = scenario.source / "docs" / "link.md"
    link.symlink_to("requirements.md")
    run_git(scenario.source, "add", "-A")
    run_git(scenario.source, "commit", "-q", "-m", "a symlinked document")
    root = Path(home["AI_HARNESS_HOME"]) / "sre-harness"
    repository = git_prs.Repository.existing(root, "example-org", "demo", str(scenario.source))
    repository.fetch_open("main", [])
    sha = repository.rev_parse(git_prs.main_ref("main"))
    diff = (
        "diff --git a/docs/link.md b/docs/link.md\n--- a/docs/link.md\n+++ b/docs/link.md\n"
        "@@ -1 +1 @@\n-requirements.md\n\\ No newline at end of file\n+../../outside.md\n"
        "\\ No newline at end of file\n"
    ).encode()
    allow = patch.Allow.from_run(run_dir(home, run_id))
    assert patch.screen(diff, allow) == ["docs/link.md"]
    with pytest.raises(patch.Refused) as raised:
        patch.admit(diff, repository, sha, allow, tmp_path / "w")
    assert raised.value.code == "mode" and "120000" in raised.value.detail
    assert not (tmp_path / "w").exists()  # refused before git ran apply
