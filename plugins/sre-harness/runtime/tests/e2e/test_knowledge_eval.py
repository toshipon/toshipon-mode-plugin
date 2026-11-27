"""`examples/synthetic/knowledge_eval.py`: the evaluation of the knowledge index on the synthetic
corpus. It runs without a model or a network, gives the same bytes on the same arguments, never
misses a mandatory record on a search that says ok, and puts a scope on every metric."""

import io
import json
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from examples.synthetic import knowledge_corpus as kc
from examples.synthetic import knowledge_eval as ke
from sre_harness import records
from sre_harness.knowledge import budget, search

REPO = Path(__file__).resolve().parents[5]
V2_SCHEMA = (
    REPO / "plugins/toshipon-mode/skills/autonomous-improvement/schemas/evaluation.schema.json"
)
SIZE_METRICS = ("input_tokens", "retrieval", "mandatory", "evidence", "decisions")
OTHER_METRICS = ("repeated_findings_proxy", "concurrency", "estimate_vs_tokenizer")


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    return ke.evaluate((50,), work_dir=tmp_path_factory.mktemp("eval"))


@pytest.fixture(scope="module")
def every_size(tmp_path_factory):
    return ke.evaluate(kc.SIZES, work_dir=tmp_path_factory.mktemp("eval-all"))


def test_the_same_arguments_give_the_same_bytes(small, tmp_path):
    again = ke.evaluate((50,), work_dir=tmp_path)
    assert records.canonical(again) == records.canonical(small)
    assert ke.markdown(again) == ke.markdown(small)


def test_no_mandatory_record_is_missed_and_the_stops_are_shown_beside_it(small):
    mandatory = small["sizes"]["50"]["mandatory"]
    assert mandatory["missed_with_status_ok"] == 0
    assert mandatory["stopped_tasks"] == 1  # the overflowing task, TASK-0001
    assert mandatory["stopped_rate"] == round(1 / 30, 4)
    assert mandatory["stops"]["mandatory_overflow"] == 1


def test_every_metric_carries_what_it_may_claim(small):
    scopes = small["claim_scope"]
    for name in (*SIZE_METRICS, *OTHER_METRICS):
        assert scopes[name].strip(), name
    assert set(small["sizes"]["50"]) - {"records", "tasks"} == set(SIZE_METRICS)
    text = ke.markdown(small)
    assert ke.SYNTHETIC_NOTE in text
    rows = [line for line in text.splitlines() if line.startswith("| ") and "---" not in line]
    labels = set(ke.SCOPE_LABELS.values())
    body = [row for row in rows if not row.startswith("| 指標 ")]
    assert body and all(row.rstrip(" |").rsplit("| ", 1)[-1] in labels for row in body)
    for name, label in ke.SCOPE_LABELS.items():
        if f"| {label} |" in text:
            assert f"- {label}: {scopes[name]}" in text


def test_no_single_ratio_and_no_time_is_printed(small):
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            for key, inner in value.items():
                keys.add(key)
                walk(inner)
        elif isinstance(value, list):
            for inner in value:
                walk(inner)

    walk(small)
    assert not {key for key in keys if any(word in key for word in ("reduction", "saving"))}
    assert not {
        key
        for key in keys
        if any(word in key for word in ("seconds", "duration", "elapsed", "_ms"))
    }


def test_paraphrased_and_literal_tasks_are_reported_apart(small):
    retrieval = small["sizes"]["50"]["retrieval"]
    assert retrieval["paraphrase"]["tasks"] == len(kc.scenario(50).traps["paraphrase_tasks"]) == 5
    assert retrieval["literal"]["tasks"] == 25
    assert (
        retrieval["paraphrase"]["relevant"]
        == retrieval["paraphrase_with_literal_query"]["relevant"]
    )
    assert retrieval["paraphrase"]["recall"] < retrieval["paraphrase_with_literal_query"]["recall"]
    assert retrieval["applicable_precision"] == 1.0  # nothing out of scope or unreadable


def test_evidence_reads_back_and_the_planted_mismatches_are_found(small):
    evidence = small["sizes"]["50"]["evidence"]
    assert evidence["fragments_read_back"] > 0
    assert evidence["hash_match_rate"] == 1.0
    assert evidence["planted_mismatches_detected"] == evidence["planted_mismatches"] == 2
    assert evidence["instruction_text_flagged"] and evidence["instruction_text_record_unchanged"]


def test_the_proxy_the_queue_and_the_concurrency_checks(small):
    proxy = small["repeated_findings_proxy"]
    assert (proxy["flagged_hunks"], proxy["rule_in_search_result"]) == (15, 15)
    assert "代理" in proxy["note"]
    queue = small["sizes"]["50"]["decisions"]
    assert queue == {"puts": 36, "accepted": 30, "duplicates_dropped": 6, "open": 30}
    flow = small["concurrency"]
    assert all(flow[key] for key in ke.CONSISTENCY)
    assert flow["reader_errors"] == flow["records_left_by_stopped_writer"] == 0


def test_the_full_load_grows_with_the_size_and_the_search_stops_at_its_budget(every_size):
    medians = [every_size["sizes"][str(n)]["input_tokens"]["full_load"]["median"] for n in kc.SIZES]
    assert medians == sorted(medians) and len(set(medians)) == len(medians)
    for size in kc.SIZES:
        value = every_size["sizes"][str(size)]
        assert value["mandatory"]["missed_with_status_ok"] == 0
        assert value["input_tokens"]["search_and_fetch"]["max"] <= budget.DEFAULT_LIMIT
        assert value["input_tokens"]["search_only"]["median"] <= search.DEFAULT_MAX_TOKENS


def test_another_search_method_gets_the_same_metrics(tmp_path, monkeypatch):
    """A method is one entry of METHODS: here one that never returns an advisory record."""

    def mandatory_only(root, principal, request, now):
        result = search.run(root, principal, request, now)
        if result["status"] == "ok":
            result["records"] = [r for r in result["records"] if r["enforcement"] == "mandatory"]
        return result

    monkeypatch.setitem(ke.METHODS, "mandatory-only", mandatory_only)
    other = ke.evaluate((50,), method="mandatory-only", work_dir=tmp_path)
    assert other["method"] == "mandatory-only"
    retrieval = other["sizes"]["50"]["retrieval"]
    assert retrieval["literal"]["precision"] == 1.0
    assert other["sizes"]["50"]["mandatory"]["missed_with_status_ok"] == 0


def test_the_evaluator_output_follows_the_v2_schema(small):
    from jsonschema import Draft202012Validator

    schema = json.loads(V2_SCHEMA.read_text(encoding="utf-8"))
    output = ke.evaluator_output(small)
    validator = Draft202012Validator({**schema, "$ref": "#/$defs/output"})
    assert list(validator.iter_errors(output)) == []
    assert output["metrics"]["mandatory_missed_50"]["value"] == 0.0
    assert all(metric["rationale"] for metric in output["metrics"].values())


def test_the_script_prints_json_markdown_and_the_evaluator_output(tmp_path):
    for fmt, check in (
        ("json", lambda text: json.loads(text)["synthetic"] is True),
        ("markdown", lambda text: text.startswith(ke.SYNTHETIC_NOTE)),
        ("evaluator-output", lambda text: "metrics" in json.loads(text)),
    ):
        out = io.StringIO()
        with redirect_stdout(out):
            ke.main(["--size", "50", "--format", fmt, "--work-dir", str(tmp_path)])
        assert check(out.getvalue()), fmt
    assert list(tmp_path.iterdir()) == []  # the temporary state root is gone
