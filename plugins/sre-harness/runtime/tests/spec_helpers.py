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


# analyze and report ------------------------------------------------------------------------

REPO_ARGS = ["--repo", "example-org/demo"]


def analyze(env, run_id, scenario, *extra):
    argv = ["spec", "analyze", "--run", run_id, "--prs", "open", *REPO_ARGS]
    argv += ["--fixture", str(scenario.fixture), "--git-source", str(scenario.source), *extra]
    return run_cli(argv, env)


def report(env, run_id, *extra):
    return run_cli(["spec", "report", "--run", run_id, *extra], env)


def extracted(env, scenario):
    """ingest and extract (stub) the scenario's workspace. Returns the run id."""
    run_id = ingested(env, scenario.manifest)
    code, result = extract(env, run_id)
    assert code == 0, result
    return run_id


def analyzed(env, scenario, *extra, expect=0):
    run_id = extracted(env, scenario)
    code, summary = analyze(env, run_id, scenario, *extra)
    assert code == expect, summary
    return run_id, summary, read(env, run_id, "findings.json")


def by_kind(found, kind, with_prs=None):
    """Findings of one kind; with_prs=True keeps those that name a PR, False those that do not."""
    items = [item for item in found["findings"] if item["kind"] == kind]
    if with_prs is None:
        return items
    return [item for item in items if bool(item["prs"]) is with_prs]


def counting_git(monkeypatch):
    """Count the processes guard starts, and the `git diff <base> <head>` among them."""
    import subprocess

    from sre_harness import guard

    seen = []

    def run(argv, **options):
        seen.append(list(argv))
        return subprocess.run(argv, **options)

    monkeypatch.setattr(guard, "_run", run)
    return seen
