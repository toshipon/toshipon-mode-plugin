"""Helpers for the spec tests: run ingest and extract through the CLI, read the registries."""

import json

from tests.helpers import read_jsonl, run_cli, run_dir

RUN_ID = "run-20260115T000000Z-0000abcd"


def ingest(env, manifest, *extra):
    return run_cli(["spec", "ingest", "--manifest", str(manifest), *extra], env)


def extract(env, run_id, *extra, backend="stub"):
    return run_cli(["spec", "extract", "--run", run_id, "--backend", backend, *extra], env)


def documents(env, run_id):
    return read_jsonl(run_dir(env, run_id) / "documents.jsonl")


def read(env, run_id, name):
    return json.loads((run_dir(env, run_id) / name).read_text(encoding="utf-8"))


def by_external(registry):
    return {item["external_id"]: item for item in registry["decisions"] if "external_id" in item}


def ingested(env, manifest, *extra):
    code, summary = ingest(env, manifest, *extra)
    assert code in (0, 1), summary
    return summary["run_id"]


def decision(number, status="approved", supersedes=(), approvers=None, basis="explicit", **extra):
    """A decision draft as `registry.build` takes it: `supersedes` may name external ids."""
    approvers = [{"name": "alice", "role": "tech lead"}] if approvers is None else approvers
    value = {
        "schema_version": 1,
        "id": f"DEC-{number:04d}",
        "external_id": f"ADR-{number:04d}",
        "run_id": RUN_ID,
        "created_at": "2026-01-15T00:00:00Z",
        "title": f"decision {number}",
        "content": f"decision {number}",
        "rationale": "",
        "status": status,
        "approvers": approvers,
        "decided_at": "2026-01-10",
        "evidence": [{"id": "E1", "ref": "DOC-0001#S1", "quote": f"status: {status}"}],
        "scope": {"systems": [], "components": [], "environments": []},
        "supersedes": list(supersedes),
        "basis": basis,
        "confidence": "high" if basis == "explicit" else "low",
    }
    value.update(extra)
    return value
