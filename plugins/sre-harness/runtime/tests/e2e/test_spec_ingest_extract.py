"""ingest (files and a GitHub fixture) -> extract (stub) through the CLI, with no network.

The stub backend makes this deterministic. It proves the plumbing and the deterministic parts
(explicit extraction, quotes, supersession, the invariants, rule-based ambiguity), not the
quality of a model's inferences.
"""

from examples.synthetic.spec_scenarios import (
    HTTP_CLIENT_NEW,
    RETENTION_CHAIN,
    RETENTION_REQ,
    write_github_fixture,
    write_workspace,
)
from sre_harness import records
from sre_harness.spec import registry
from tests.helpers import read_jsonl, run_dir
from tests.spec_helpers import extract, ingest, read


def test_spec_ingest_and_extract_run_end_to_end(tmp_path, home):
    manifest = write_workspace(tmp_path / "ws", github=True)
    fixture = write_github_fixture(tmp_path / "fx")
    code, summary = ingest(home, manifest, "--fixture", str(fixture))
    assert code == 1 and summary["status"] == "partial"  # docs/absent.md is listed, not there
    assert summary["documents"] == 15
    run_id = summary["run_id"]

    code, result = extract(home, run_id)
    assert code == 0, result
    decisions = read(home, run_id, "decisions.json")
    requirements = read(home, run_id, "requirements.json")
    assert records.violations("decision", decisions, "registry") == []
    assert records.violations("requirement", requirements, "registry") == []
    assert registry.check(decisions["decisions"]) == []
    assert decisions["active"] == registry.active(decisions["decisions"])

    external = {d.get("external_id"): d["id"] for d in decisions["decisions"]}
    assert external[RETENTION_CHAIN[-1]] in decisions["active"]
    assert external[HTTP_CLIENT_NEW] in decisions["active"]
    assert not {external[name] for name in RETENTION_CHAIN[:-1]} & set(decisions["active"])

    ids = [r["id"] for r in requirements["requirements"]]
    assert RETENTION_REQ in ids and "REQ-0006" in ids  # REQ-0006 comes from the PR body
    assert len(ids) == len(set(ids))
    assert result["decisions"] == len(decisions["decisions"])
    assert result["requirements"] == len(ids)

    directory = run_dir(home, run_id)
    for row in read_jsonl(directory / "documents.jsonl"):
        assert records.violations("spec-document", row) == []
    for row in read_jsonl(directory / "llm-calls.jsonl"):
        assert records.violations("llm-call", row) == []
        assert row["stage"] == "extract" and row["charged_usd"] == 0
