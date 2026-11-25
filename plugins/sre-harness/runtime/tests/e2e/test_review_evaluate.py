"""collect -> normalize -> split -> analyze -> classify -> generate -> evaluate through the CLI.

Stub backends make this deterministic. It proves the plumbing, the holdout, the interpreter,
the labels and the metrics on synthetic data. It says nothing about how good an LLM's lessons,
candidates or judgements would be, and synthetic numbers are not a claim of accuracy.
"""

import json
from pathlib import Path

from examples.synthetic.github_responses import SPLIT_T1, TIMEOUT_FINDING, evaluation_scenario
from sre_harness import llm, records
from tests.helpers import RULES_TEXT, collect_fixture, read_jsonl, run_cli, run_dir

STUB = ["--backend", "stub"]


def test_the_review_learning_evaluation_runs_end_to_end(tmp_path, home, clock):
    fixture = evaluation_scenario(injected_exception=True).write(tmp_path / "fx")
    code, summary = collect_fixture(fixture, home)
    assert code == 0 and summary["pulls"] == 16
    run_id = summary["run_id"]
    assert run_cli(["review", "normalize", "--run", run_id], home)[0] == 0

    code, made = run_cli(["review", "split", "--run", run_id, "--t1", SPLIT_T1], home)
    assert code == 0 and (made["generation_prs"], made["evaluation_prs"]) == (13, 3)
    split = ["--split", made["split"]]

    code, analyzed = run_cli(["review", "analyze", "--run", run_id, *STUB, *split], home)
    assert code == 0 and analyzed["lessons"] == 6
    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "http.md").write_text(RULES_TEXT)
    argv = ["review", "classify", "--run", run_id, "--existing-rules", str(rules), *STUB]
    code, classified = run_cli(argv, home)
    # The outside account's reply quoted by the timeout lesson sets it aside (untrusted_quote).
    assert code == 0 and classified["classified"] == 1

    argv = ["review", "generate", "--run", run_id, *STUB, *split]
    code, generated = run_cli([*argv, "--include-untrusted"], home)
    assert code == 0 and generated["candidates"] == 4 and generated["include_untrusted"]

    argv = ["review", "evaluate", "--run", run_id, *STUB, *split]
    code, evaluated = run_cli(argv, home)
    assert code == 0 and evaluated["status"] == "unevaluable"
    assert evaluated["with_candidates"]["true_positives"] == 1
    assert evaluated["with_candidates"]["findings_on_clean"] == 0

    directory = run_dir(home, run_id)
    for name, schema in (
        ("split.json", "review-split"),
        ("evaluation.json", "review-evaluation"),
    ):
        assert records.violations(schema, json.loads((directory / name).read_text())) == []
    for folder in sorted((directory / "candidates").iterdir()):
        record = json.loads((folder / "candidate.json").read_text())
        assert records.violations("improvement-candidate", record) == []
        assert record["status"] == "evaluated"
        assert record["artifact"]["content_sha256"] == records.sha256(
            (folder / "body.md").read_bytes()
        )
    made = [
        json.loads((f / "candidate.json").read_text()) for f in (directory / "candidates").iterdir()
    ]
    (timeout,) = [record for record in made if record["problem"] == TIMEOUT_FINDING]
    assert timeout["untrusted_included"] is True
    assert {"imperative_to_agent", "url"} <= {item["kind"] for item in timeout["lint"]}
    stages = [row["stage"] for row in read_jsonl(directory / llm.CALLS_FILE)]
    assert stages == ["analyze", "classify", "generate", "generate", "evaluate"]
    assert llm.spent_usd(directory) == 0
    assert (directory / "evaluation.md").read_text().startswith("# Review evaluation")
    assert Path(made["split"]).parent == directory
