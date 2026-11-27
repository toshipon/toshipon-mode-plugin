"""Helpers shared by the knowledge tests. Everything here is synthetic."""

import copy
import json
from pathlib import Path

from sre_harness import paths, records
from tests.helpers import run_cli

UPDATED_AT = "2026-11-01T00:00:00Z"
APPROVAL = {"approver": "alice", "decided_at": "2026-10-01T00:00:00Z", "via": "review approve"}
SHA = "0" * 64

CONFIG = {
    "schema_version": 1,
    "projects": {
        "project-alpha": {
            "repos": ["example-org/alpha"],
            "default_access": {"classification": "internal", "readers": ["team-alpha"]},
            "components": {"queue": {"path_globs": ["infra/queue/**"]}},
        },
        "project-gamma": {
            "repos": ["example-org/gamma"],
            "default_access": {"classification": "confidential", "readers": ["team-gamma"]},
            "components": {},
        },
    },
    "principals": {
        "workspace-a": {
            "readers": ["everyone", "team-alpha", "team-gamma"],
            "classifications": ["public", "synthetic", "internal", "confidential"],
        },
        "workspace-b": {
            "readers": ["everyone", "team-alpha"],
            "classifications": ["public", "synthetic", "internal"],
        },
    },
}


def item(key, **changes):
    """An approved, advisory, project-level item of project-alpha readable by team-alpha."""
    value = {
        "origin": {"kind": "synthetic", "key": key},
        "kind": "rule",
        "scope": {
            "level": "project",
            "project_id": "project-alpha",
            "component_id": None,
            "task_id": None,
        },
        "facets": {"topic": ["retention"], "resource_type": ["queue"]},
        "access": {"classification": "internal", "readers": ["team-alpha"]},
        "summary": f"{key}: キューのメッセージ保持期間は 14 日にする",
        "applies_when": ["キューを新しく作る"],
        "exceptions": [],
        "status": "approved",
        "enforcement": "advisory",
        "evidence": [
            {"locator": f"doc:synthetic:{key}", "revision": "r1", "content_sha256": SHA, "size": 10}
        ],
        "updated_at": UPDATED_AT,
        "approval": copy.deepcopy(APPROVAL),
    }
    value.update(changes)
    return value


def source_file(items, *, source="synthetic", complete=True, tombstones=None):
    value = {"schema_version": 1, "source": source, "complete": complete, "items": items}
    if tombstones is not None:
        value["tombstones"] = tombstones
    return value


def write_input(path, items, **options):
    path = Path(path)
    path.write_text(json.dumps(source_file(items, **options), ensure_ascii=False))
    return path


def state_root(env):
    return paths.state_root(env)


def knowledge_dir(env):
    return Path(env["AI_HARNESS_HOME"]) / "sre-harness" / "knowledge"


def write_config(env, value=None):
    directory = knowledge_dir(env)
    paths.write_private(directory / "config.json", records.canonical(value or CONFIG))
    return directory / "config.json"


def update(env, input_path, *extra):
    return run_cli(
        ["knowledge", "update", "--source", "file", "--input", str(input_path), *extra], env
    )


# Source texts behind evidence locators --------------------------------------------------------

RUN_ID = "run-20261127T000000Z-0000abcd"
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "alice",
    "GIT_AUTHOR_EMAIL": "alice@example.com",
    "GIT_COMMITTER_NAME": "alice",
    "GIT_COMMITTER_EMAIL": "alice@example.com",
    "GIT_AUTHOR_DATE": "2026-11-01T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-11-01T00:00:00Z",
}


def evidence(locator, text, revision="v0001"):
    return {
        "locator": locator,
        "revision": revision,
        "content_sha256": records.sha256(text),
        "size": len(text.encode("utf-8")),
    }


def registry_evidence(env, entry, text, *, written=None):
    """Write `written` (default `text`) as registry/<entry>/v0001.md; evidence records `text`."""
    path = paths.child(state_root(env), "registry", entry, "v0001.md")
    paths.write_private(path, text if written is None else written)
    return evidence(f"registry:{entry}/v0001.md", text)


def run_evidence(env, document, section, text, *, run_id=RUN_ID):
    """Add one section to the synthetic run's documents.jsonl; evidence records its text."""
    path = paths.child(state_root(env), "runs", run_id, "documents.jsonl")
    rows = records.read_jsonl(path) if path.exists() else []
    rows.append(
        {
            "schema_version": 1,
            "id": document,
            "run_id": run_id,
            "created_at": UPDATED_AT,
            "kind": "markdown",
            "title": document,
            "sections": [
                {"id": section, "heading": document, "text": text, "locator": "docs/x.md"}
            ],
            "provenance": {
                "source_id": "synthetic",
                "locator": "docs/x.md",
                "content_sha256": records.sha256(text),
                "retrieved_at": UPDATED_AT,
            },
            "missing": [],
        }
    )
    records.write_jsonl(path, rows)
    return evidence(f"run:{run_id}/documents.jsonl#{document}#{section}", text, revision=run_id)


def bare_repository(env, work, repo, files):
    """A commit of `files` ({path: text}) in a bare repository where `spec analyze` keeps them
    (`cache/git/<owner>--<name>--<key>.git`). Returns the commit sha. Tests may start git;
    the package may not."""
    import os
    import subprocess

    child = {**os.environ, **GIT_ENV}
    work = Path(work)
    work.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True, env=child)
    for name, text in files.items():
        target = work / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True, env=child)
    subprocess.run(["git", "-C", str(work), "commit", "-q", "-m", "x"], check=True, env=child)
    sha = subprocess.run(
        ["git", "-C", str(work), "rev-parse", "HEAD"],
        check=True,
        env=child,
        capture_output=True,
        text=True,
    ).stdout.strip()
    owner, name = repo.split("/")
    key = records.sha256(f"{repo}\n{work}")[:12]
    git_dir = state_root(env) / "cache" / "git" / f"{owner}--{name}--{key}.git"
    git_dir.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(git_dir)], check=True, env=child)
    return sha


def git_evidence(path, sha, text, start=None, end=None):
    """Evidence for `git:<path>[#L<a>[-L<b>]]`, hashed over the cited lines as #52 stamps it."""
    locator = f"git:{path}"
    cited = text
    if start is not None:
        end = start if end is None else end
        locator += f"#L{start}" + (f"-L{end}" if end != start else "")
        cited = "\n".join(text.splitlines()[start - 1 : end])
    return evidence(locator, cited, revision=sha)


def fetch(env, *argv):
    return run_cli(["knowledge", "fetch", *argv], env)


# A synthetic spec run (registries written directly, as `spec extract` and `analyze` write them) --

SPEC_RUN_ID = "run-20261120T000000Z-0000beef"
SPEC_CREATED_AT = "2026-11-20T00:00:00Z"
HEAD_SHA = "1" * 40


def spec_decision(number, status="approved", supersedes=(), basis="explicit", **extra):
    """A decision draft of document DOC-<number>: `supersedes` names external ids."""
    from tests.spec_helpers import decision

    value = decision(number, status=status, supersedes=supersedes, basis=basis)
    value["evidence"] = [{"id": "E1", "ref": f"DOC-{number:04d}#S1", "quote": f"status: {status}"}]
    if basis == "inferred":
        value["id"] = f"DCI-{number:04d}"
        value.pop("external_id")
    value.update(extra)
    return value


def spec_requirement(number, basis="explicit", decision_refs=(), **extra):
    explicit = basis == "explicit"
    value = {
        "schema_version": 1,
        "id": f"{'REQ' if explicit else 'RQI'}-{number:04d}",
        "run_id": SPEC_RUN_ID,
        "created_at": SPEC_CREATED_AT,
        "title": f"requirement {number}",
        "statement": f"要件 {number} の文",
        "acceptance_criteria": [],
        "rationale": "",
        "evidence": [{"id": "E1", "ref": f"DOC-{100 + number:04d}#S1", "quote": "要件"}],
        "decision_refs": list(decision_refs),
        "nfr_category": None,
        "ambiguity": [],
        "basis": basis,
        "confidence": "high" if explicit else "low",
    }
    if explicit:
        value["external_id"] = value["id"]
    value.update(extra)
    return value


def spec_link(number, source, target, relation, basis="explicit"):
    return {
        "schema_version": 1,
        "id": f"TL-{number:04d}",
        "run_id": SPEC_RUN_ID,
        "created_at": SPEC_CREATED_AT,
        "from": {"type": source[0], "id": source[1]},
        "to": {"type": target[0], "id": target[1]},
        "relation": relation,
        "basis": basis,
        "evidence": {"ref": f"{HEAD_SHA}:docs/x.md#L1"},
        "confidence": "high" if basis == "explicit" else "low",
    }


def section_text(document):
    return f"{document} の本文。合成データです。"


def write_spec_run(env, drafts, requirements=(), links=None, *, run_id=SPEC_RUN_ID, built=None):
    """Write decisions.json (from `drafts` through `registry.build`, or `built` as it is),
    requirements.json, trace.json (when `links` is given) and documents.jsonl with one section
    per evidence ref. Returns the run id."""
    from sre_harness.spec import registry as spec_registry

    def own(value):
        return {**value, "run_id": run_id}

    decisions = built if built is not None else spec_registry.build([own(d) for d in drafts])
    head = {"schema_version": 1, "run_id": run_id, "created_at": SPEC_CREATED_AT}
    directory = paths.child(state_root(env), "runs", run_id)
    records.write_json(directory / "decisions.json", {**head, **decisions})
    records.write_json(
        directory / "requirements.json",
        {**head, "requirements": [own(r) for r in requirements], "input_missing": []},
    )
    if links is not None:
        trace = {
            **head,
            "default_branch": {"name": "main", "sha": HEAD_SHA},
            "links": [own(link) for link in links],
        }
        records.write_json(directory / "trace.json", trace)
    refs = sorted(
        {
            evidence["ref"]
            for item in [*decisions["decisions"], *requirements]
            for evidence in item["evidence"]
        }
    )
    rows = []
    for ref in refs:
        document, section = ref.split("#")
        rows.append(
            {
                "schema_version": 1,
                "id": document,
                "run_id": run_id,
                "created_at": SPEC_CREATED_AT,
                "kind": "adr",
                "title": document,
                "sections": [
                    {
                        "id": section,
                        "heading": document,
                        "text": section_text(document),
                        "locator": f"docs/{document}.md",
                    }
                ],
                "provenance": {
                    "source_id": "synthetic",
                    "locator": f"docs/{document}.md",
                    "content_sha256": records.sha256(section_text(document)),
                    "retrieved_at": SPEC_CREATED_AT,
                },
                "missing": [],
            }
        )
    records.write_jsonl(directory / "documents.jsonl", rows)
    return run_id


def update_spec(env, run_id=SPEC_RUN_ID, project="project-alpha", *extra):
    argv = ["knowledge", "update", "--source", "spec-run", "--run", run_id, "--project", project]
    return run_cli([*argv, *extra], env)
