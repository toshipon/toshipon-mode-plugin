"""`spec ingest`: files and GitHub to documents.jsonl, read only, bounded, nothing filled in."""

import json
import os

import pytest

from examples.synthetic.spec_scenarios import (
    ABSENT_FILE,
    CONFLUENCE,
    PULL,
    PULL_HEAD,
    REPO,
    SLACK,
    manifest,
    write_github_fixture,
    write_workspace,
)
from sre_harness import guard, records
from sre_harness.spec import ingest_file
from tests.helpers import run_dir
from tests.spec_helpers import documents, ingest, read

# Front-matter -----------------------------------------------------------------------------


def test_front_matter_reads_scalars_and_simple_lists():
    text = (
        "---\n"
        "id: ADR-0005\n"
        "Status: Approved\n"
        'title: "HTTP クライアント"\n'
        "supersedes: [ADR-0002, 'ADR-0003']\n"
        "approvers:\n"
        "  - alice (tech lead)\n"
        "  - bob (sre)\n"
        "# a comment\n"
        "empty:\n"
        "---\n"
        "# Body\n"
    )
    parsed = ingest_file.front_matter(text)
    assert parsed["fields"] == {
        "id": "ADR-0005",
        "status": "Approved",
        "title": "HTTP クライアント",
        "supersedes": ["ADR-0002", "ADR-0003"],
        "approvers": ["alice (tech lead)", "bob (sre)"],
        "empty": "",
    }
    assert parsed["missing"] == []
    assert parsed["body"] == "# Body\n"
    assert parsed["body_line"] == 12
    assert parsed["raw"].startswith("---\nid: ADR-0005")


@pytest.mark.parametrize(
    "block,missing",
    [
        ("nested:\n  key: value\n", ["front_matter.nested"]),
        ("story: |\n  line one\n", ["front_matter.story"]),
        ("folded: >\n  text\n", ["front_matter.folded"]),
        ("map: {a: 1}\n", ["front_matter.map"]),
        ("just a sentence\n", ["front_matter.line_2"]),
    ],
)
def test_forms_the_reader_does_not_understand_are_missing_not_guessed(block, missing):
    parsed = ingest_file.front_matter(f"---\n{block}---\nbody\n")
    assert parsed["missing"] == missing
    assert all(key not in parsed["fields"] for key in ("nested", "story", "folded", "map"))


def test_an_unterminated_front_matter_is_missing_and_the_text_stays_body():
    text = "---\nid: ADR-0001\nno end here\n"
    parsed = ingest_file.front_matter(text)
    assert parsed["fields"] == {} and parsed["raw"] is None
    assert parsed["missing"] == ["front_matter"]
    assert parsed["body"] == text and parsed["body_line"] == 1


def test_no_front_matter_is_not_missing():
    parsed = ingest_file.front_matter("# Title\n")
    assert parsed == {"fields": {}, "raw": None, "body": "# Title\n", "body_line": 1, "missing": []}


# Sections ---------------------------------------------------------------------------------


def test_markdown_sections_split_at_headings_outside_code_fences():
    text = "intro\n\n# Title\n\nbody\n```\n# not a heading\n```\n## Next\ntext\n"
    sections = ingest_file.markdown_sections(text, first_line=5)
    assert [(s["heading"], s["locator"]) for s in sections] == [
        ("", "L5-L6"),
        ("Title", "L7-L12"),
        ("Next", "L13-L14"),
    ]
    assert "# not a heading" in sections[1]["text"]
    assert sections[2]["text"] == "text"


def test_slack_export_messages_become_sections():
    sections = ingest_file.slack_sections(SLACK)
    assert [s["heading"] for s in sections] == ["carol", "bob"]
    assert sections[0]["text"] == SLACK[0]["text"]
    assert sections[1]["locator"] == "ts 1768435260.000200 thread 1768435200.000100"


@pytest.mark.parametrize(
    "value",
    [{"messages": []}, [{"text": "no ts"}], [{"ts": "1.2", "text": 3}], ["string"]],
)
def test_a_slack_export_of_another_shape_is_refused(value):
    with pytest.raises(ValueError):
        ingest_file.slack_sections(value)


def test_confluence_export_storage_becomes_sections():
    title, sections = ingest_file.confluence_sections(CONFLUENCE)
    assert title == "Runbook: log pipeline"
    assert [s["heading"] for s in sections] == ["Runbook: log pipeline", "REQ-0005: Restore drill"]
    assert "Operational notes" in sections[0]["text"]
    assert "- The drill restores one day of logs in under 4 hours." in sections[1]["text"]
    assert "<" not in sections[1]["text"]


def test_a_confluence_export_without_storage_is_refused():
    with pytest.raises(ValueError):
        ingest_file.confluence_sections({"title": "x", "body": {}})


# The stage --------------------------------------------------------------------------------


def test_ingest_writes_documents_and_records_the_absent_file(tmp_path, home):
    code, summary = ingest(home, write_workspace(tmp_path / "ws"))
    assert code == 1, summary  # a listed file was absent: the run is partial
    assert summary["status"] == "partial"
    assert summary["documents"] == 13
    info = read(home, summary["run_id"], "ingest.json")
    assert info["input_missing"] == [
        {"source_id": f"file:{ABSENT_FILE}", "locator": ABSENT_FILE, "reason": "file_not_found"}
    ]
    assert info["patch_globs"] == ["docs/**/*.md"]
    docs = documents(home, summary["run_id"])
    assert [doc["id"] for doc in docs] == [f"DOC-{n:04d}" for n in range(1, 14)]
    for doc in docs:
        assert records.violations("spec-document", doc) == [], doc["id"]
        assert ABSENT_FILE not in doc["provenance"]["locator"]
    kinds = {doc["kind"] for doc in docs}
    assert kinds == {"adr", "markdown", "minutes", "slack_export", "confluence_export"}


def test_without_absent_files_ingest_succeeds(tmp_path, home):
    code, summary = ingest(home, write_workspace(tmp_path / "ws", absent=False))
    assert code == 0 and summary["status"] == "complete"


def test_documents_keep_hash_and_front_matter(tmp_path, home):
    path = write_workspace(tmp_path / "ws", absent=False)
    run_id = ingest(home, path)[1]["run_id"]
    (adr,) = [
        d
        for d in documents(home, run_id)
        if d["provenance"]["locator"].endswith("0005-http-client-b.md")
    ]
    data = (path.parent / adr["provenance"]["locator"]).read_bytes()
    assert adr["provenance"]["content_sha256"] == records.sha256(data)
    assert adr["front_matter"]["supersedes"] == ["ADR-0002"]
    assert adr["sections"][0]["heading"] == "front-matter"
    assert adr["title"] == "HTTP クライアントを B に移行する"


def test_output_names_come_from_the_harness_not_the_input(tmp_path, home):
    run_id = ingest(home, write_workspace(tmp_path / "ws"))[1]["run_id"]
    names = sorted(path.name for path in run_dir(home, run_id).iterdir())
    assert names == ["documents.jsonl", "ingest.json"]


def write_manifest(base, files, **extra):
    base.mkdir(parents=True, exist_ok=True)
    value = {**manifest(files), **extra}
    path = base / "manifest.json"
    path.write_text(json.dumps(value))
    return path


@pytest.mark.parametrize("listed", ["../outside.md", "docs/../../outside.md", "/etc/hosts"])
def test_paths_outside_the_base_directory_are_refused(tmp_path, home, listed):
    (tmp_path / "outside.md").write_text("# outside\n")
    code, _ = ingest(home, write_manifest(tmp_path / "ws", [(listed, "markdown")]))
    assert code == 3
    assert not (tmp_path / "state" / "sre-harness" / "runs").exists()


def test_a_symlinked_file_is_not_followed(tmp_path, home):
    base = tmp_path / "ws"
    (tmp_path / "secret.md").write_text("# not for the harness\n")
    path = write_manifest(base, [("docs/link.md", "markdown")])
    (base / "docs").mkdir()
    os.symlink(tmp_path / "secret.md", base / "docs" / "link.md")
    code, _ = ingest(home, path)
    assert code == 3


def test_a_symlinked_directory_is_not_followed(tmp_path, home):
    base = tmp_path / "ws"
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "a.md").write_text("# a\n")
    path = write_manifest(base, [("docs/a.md", "markdown")])
    os.symlink(tmp_path / "real", base / "docs")
    assert ingest(home, path)[0] == 3


def test_a_symlinked_manifest_is_refused(tmp_path, home):
    real = write_manifest(tmp_path / "ws", [])
    os.symlink(real, tmp_path / "link.json")
    assert ingest(home, tmp_path / "link.json")[0] == 3


@pytest.mark.parametrize("glob", ["../docs/*.md", "/abs/*.md", ".git/config", ".github/**"])
def test_patch_globs_that_leave_the_documents_are_refused(tmp_path, home, glob):
    assert ingest(home, write_manifest(tmp_path / "ws", [], patch_globs=[glob]))[0] == 3


def test_a_document_over_the_size_cap_stops_without_truncating(tmp_path, home, monkeypatch):
    base = tmp_path / "ws"
    path = write_manifest(base, [("big.md", "markdown")])
    (base / "big.md").write_text("# big\n" + "x" * 2000)
    monkeypatch.setattr(ingest_file, "MAX_DOCUMENT_BYTES", 1024)
    code, _ = ingest(home, path)
    assert code == 1
    assert not (tmp_path / "state" / "sre-harness" / "runs").exists()


def test_the_documented_caps():
    assert ingest_file.MAX_DOCUMENT_BYTES == 2 * 1024 * 1024
    assert ingest_file.MAX_RUN_BYTES >= ingest_file.MAX_DOCUMENT_BYTES


def test_the_run_total_over_its_cap_stops(tmp_path, home, monkeypatch):
    base = tmp_path / "ws"
    path = write_manifest(base, [("a.md", "markdown"), ("b.md", "markdown")])
    for name in ("a.md", "b.md"):
        (base / name).write_text("# doc\n" + "y" * 700)
    monkeypatch.setattr(ingest_file, "MAX_RUN_BYTES", 1024)
    assert ingest(home, path)[0] == 1


def test_an_unreadable_export_is_input_missing_not_a_guess(tmp_path, home):
    base = tmp_path / "ws"
    path = write_manifest(base, [("s.json", "slack_export"), ("bad.md", "markdown")])
    (base / "s.json").write_text("{not json")
    (base / "bad.md").write_bytes(b"\xff\xfe not utf-8")
    code, summary = ingest(home, path)
    assert code == 1 and summary["documents"] == 0
    reasons = [
        entry["reason"] for entry in read(home, summary["run_id"], "ingest.json")["input_missing"]
    ]
    assert reasons == ["unreadable_export", "not_utf8"]


@pytest.mark.parametrize(
    "value",
    [{"files": []}, {"schema_version": 1, "files": [{"path": "a.md", "kind": "pdf"}]}, []],
)
def test_a_manifest_that_breaks_its_schema_is_exit_2(tmp_path, home, value):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(value))
    assert ingest(home, path)[0] == 2


def test_a_missing_manifest_is_exit_2(tmp_path, home):
    assert ingest(home, tmp_path / "nope.json")[0] == 2


# GitHub -----------------------------------------------------------------------------------


def github_manifest(tmp_path):
    return write_manifest(tmp_path / "ws", [], github=[{"repo": REPO, "pull": PULL}])


def test_github_needs_a_token_or_a_fixture(tmp_path, home):
    assert ingest(home, github_manifest(tmp_path))[0] == 2


def test_an_empty_auth_env_sends_nothing_and_exits_2(tmp_path, home, monkeypatch):
    started = []
    monkeypatch.setattr(guard, "_run", lambda *a, **k: started.append(a))
    env = {**home, "SPEC_READ_TOKEN": ""}
    code, _ = ingest(env, github_manifest(tmp_path), "--auth-env", "SPEC_READ_TOKEN")
    assert code == 2 and started == []
    assert not (tmp_path / "state" / "sre-harness" / "runs").exists()


def test_github_pr_body_and_comments_from_a_fixture(tmp_path, home):
    fixture = write_github_fixture(tmp_path / "fx")
    code, summary = ingest(home, github_manifest(tmp_path), "--fixture", str(fixture))
    assert code == 0, summary
    pr, comments = documents(home, summary["run_id"])
    assert pr["kind"] == "github_pr" and comments["kind"] == "github_issue_comment"
    assert pr["provenance"]["sha"] == PULL_HEAD
    assert pr["provenance"]["source_id"] == f"github:{REPO}#{PULL}"
    assert [s["heading"] for s in pr["sections"]] == ["概要", "REQ-0006: 削除ジョブの監視"]
    assert [s["locator"] for s in comments["sections"]] == [
        "issue_comment 2001",
        "issue_comment 2002",
    ]
    for doc in (pr, comments):
        assert records.violations("spec-document", doc) == []


def test_github_through_gh_uses_only_get_endpoints(tmp_path, home, fake_gh):
    seen = fake_gh(write_github_fixture(tmp_path / "fx"))
    env = {**home, "SPEC_READ_TOKEN": "synthetic-value-0123456789"}
    code, _ = ingest(env, github_manifest(tmp_path), "--auth-env", "SPEC_READ_TOKEN")
    assert code == 0
    assert [call.argv[-1] for call in seen] == [
        f"repos/{REPO}/pulls/{PULL}",
        f"repos/{REPO}/issues/{PULL}/comments?page=1&per_page=100",
    ]
    assert all("--method" in call.argv and "GET" in call.argv for call in seen)


def test_a_pr_that_cannot_be_read_is_input_missing(tmp_path, home):
    fixture = write_github_fixture(tmp_path / "fx", pull_status=404)
    code, summary = ingest(home, github_manifest(tmp_path), "--fixture", str(fixture))
    assert code == 1
    (entry,) = read(home, summary["run_id"], "ingest.json")["input_missing"]
    assert entry["reason"] == "http_404" and entry["source_id"] == f"github:{REPO}#{PULL}"


def test_a_pr_without_a_body_lists_it_as_missing(tmp_path, home):
    fixture = write_github_fixture(tmp_path / "fx", body=None)
    code, summary = ingest(home, github_manifest(tmp_path), "--fixture", str(fixture))
    assert code == 0
    pr = documents(home, summary["run_id"])[0]
    assert pr["sections"] == [] and pr["missing"] == ["body"]
