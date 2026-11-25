"""collect -> normalize -> analyze (stub) -> classify (stub) through the CLI, with no network.

The stub backend makes this deterministic. It proves the plumbing and the deterministic checks
(quotes, ids, timestamps, trust, confidence, set-aside, conflicts), not the quality of an LLM.
"""

from examples.synthetic.github_responses import review_learning_scenario
from sre_harness import llm, records
from tests.helpers import collect_fixture, read_jsonl, run_cli, run_dir

RULES = "# HTTP\n\n- Every outbound HTTP client call must set a timeout.\n"


def test_the_review_learning_stages_run_end_to_end(tmp_path, home, clock):
    fixture = review_learning_scenario(injection=True).write(tmp_path / "fx")
    code, summary = collect_fixture(fixture, home)
    assert code == 0 and summary["pulls"] == 13
    run_id = summary["run_id"]

    code, normalized = run_cli(["review", "normalize", "--run", run_id], home)
    assert code == 0 and normalized["rejected"] == 0

    code, analyzed = run_cli(["review", "analyze", "--run", run_id, "--backend", "stub"], home)
    assert code == 0
    assert analyzed["lessons"] == 6 and analyzed["rejected"] == 0
    assert analyzed["fix"] == {"found": 6, "not_found": 7, "ambiguous": 0}

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "http.md").write_text(RULES)
    argv = ["review", "classify", "--run", run_id, "--existing-rules", str(rules)]
    code, result = run_cli([*argv, "--backend", "stub"], home)
    assert code == 0
    assert (result["classified"], result["set_aside"]) == (2, 4)
    assert result["conflicts"] >= 1

    directory = run_dir(home, run_id)
    checks = {
        "threads-with-fix.jsonl": ("review-thread", None),
        "lessons.jsonl": ("review-lesson", None),
        "classified.jsonl": ("review-classified", None),
        "set-aside.jsonl": ("review-classified", "set_aside"),
        "conflicts.jsonl": ("review-conflict", None),
        llm.CALLS_FILE: ("llm-call", None),
    }
    for name, (schema, definition) in checks.items():
        rows = read_jsonl(directory / name)
        assert rows, name
        for row in rows:
            assert records.violations(schema, row, definition) == [], name
            assert row["run_id"] == run_id
    stages = [row["stage"] for row in read_jsonl(directory / llm.CALLS_FILE)]
    assert stages == ["analyze", "classify"]
    assert llm.spent_usd(directory) == 0
