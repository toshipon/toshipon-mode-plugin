"""Collect and normalize the default synthetic scenario through the CLI, with no network."""

import stat

from examples.synthetic.github_responses import default_scenario
from sre_harness import records
from tests.helpers import collect_fixture, read_jsonl, run_cli, run_dir


def test_collect_then_normalize_gives_schema_valid_threads(tmp_path, home, clock):
    code, summary = collect_fixture(default_scenario().write(tmp_path / "fx"), home)
    assert code == 0
    assert summary["status"] == "complete" and summary["pulls"] == 3
    run_id = summary["run_id"]
    assert records.violations("common", run_id, "run_id") == []

    code, result = run_cli(["review", "normalize", "--run", run_id], home)
    assert code == 0 and result["rejected"] == 0
    directory = run_dir(home, run_id)
    threads = read_jsonl(directory / "threads.jsonl")
    assert len(threads) == result["threads"] == 8
    for thread in threads:
        assert records.violations("review-thread", thread) == []
        assert thread["run_id"] == run_id and thread["repo"] == "example-org/demo"
        assert "fix" not in thread  # filled by a later stage
    assert [thread["id"] for thread in threads] == [f"RT-{n:04d}" for n in range(1, 9)]
    kinds = [thread["kind"] for thread in threads]
    assert kinds.count("inline_thread") == 3
    assert kinds.count("review_summary") == 3
    assert kinds.count("issue_comment") == 2
    assert {thread["pr"]["number"] for thread in threads} == {1, 2, 3}  # PR 4 is too old
    for name in ("collect.json", "threads.jsonl", "rejected.jsonl"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
