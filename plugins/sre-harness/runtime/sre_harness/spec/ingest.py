"""`spec ingest`: the manifest's files and GitHub PRs to `documents.jsonl` in a new run.

Everything is read and checked before the run is created, so a refused path, a symlink or a
size cap leaves no run behind. Gaps (a listed file that is absent, a PR that answers 404) do
not stop the stage: they go to `ingest.json`'s `input_missing`, the run is `partial`, and the
command exits 1 after writing what it could read.
"""

import json
import os
from pathlib import Path

from .. import paths, records
from ..errors import BoundaryError, InputError, QualityGateError
from . import ingest_file, ingest_github

MAX_MANIFEST_BYTES = 1024 * 1024


def load_manifest(path):
    """(manifest, base directory, sha256). The manifest itself is held to the same rules."""
    path = Path(os.path.abspath(path))
    if path.is_symlink():
        raise BoundaryError(f"refused to follow a symlinked manifest: {path}")
    if not path.is_file():
        raise InputError(f"--manifest is not a file: {path}")
    data = path.read_bytes()
    if len(data) > MAX_MANIFEST_BYTES:
        raise InputError(f"the manifest is over {MAX_MANIFEST_BYTES} bytes.")
    try:
        manifest = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as error:
        raise InputError(f"the manifest is not JSON: {error}") from None
    records.validate("spec-manifest", manifest)
    for entry in manifest["files"]:
        ingest_file.relative_parts(entry["path"])
    for glob in manifest.get("patch_globs", []):
        ingest_file.check_patch_glob(glob)
    return manifest, path.parent, records.sha256(data)


def ingest(manifest_path, transport, limiter, source, env):
    """Run the stage. Returns the summary printed on stdout."""
    manifest, base, manifest_sha = load_manifest(manifest_path)
    if manifest.get("github") and transport is None:
        raise InputError("the manifest lists GitHub pull requests: pass --auth-env or --fixture.")
    now = records.iso()
    drafts, missing, total = [], [], 0
    for entry in manifest["files"]:
        try:
            document, size = ingest_file.read_file(base, entry, now)
        except ingest_file.Unreadable as gap:
            locator = "/".join(ingest_file.relative_parts(entry["path"]))
            missing.append(
                {"source_id": f"file:{locator}", "locator": locator, "reason": gap.reason}
            )
            continue
        total += size
        drafts.append(document)
    for entry in manifest.get("github", []):
        documents, gaps = ingest_github.read_pull(transport, limiter, entry, now)
        for document in documents:
            size = len(records.canonical(document["sections"]).encode("utf-8"))
            ingest_file.check_size(size, document["provenance"]["locator"])
            total += size
        drafts += documents
        missing += gaps
    if total > ingest_file.MAX_RUN_BYTES:
        raise QualityGateError(
            f"the documents add up to {total} bytes, over the {ingest_file.MAX_RUN_BYTES}-byte "
            "cap per run; stopped instead of dropping some."
        )

    run_dir = paths.create_run(paths.state_root(env))
    run_id, rows = run_dir.name, []
    for number, draft in enumerate(drafts, start=1):
        row = {
            "schema_version": records.SCHEMA_VERSION,
            "id": records.record_id("DOC", number),
            "run_id": run_id,
            "created_at": now,
            **draft,
        }
        records.validate("spec-document", row)
        rows.append(row)
    records.write_jsonl(paths.child(run_dir, "documents.jsonl"), rows)
    status = "partial" if missing else "complete"
    record = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": run_id,
        "created_at": now,
        "status": status,
        "source": source,
        "manifest_sha256": manifest_sha,
        "base_dir": str(base),
        "patch_globs": [ingest_file.check_patch_glob(g) for g in manifest.get("patch_globs", [])],
        "documents": [
            {"id": row["id"], "kind": row["kind"], "source_id": row["provenance"]["source_id"]}
            for row in rows
        ],
        "bytes": total,
        "input_missing": missing,
    }
    records.write_json(paths.child(run_dir, "ingest.json"), record)
    return {
        "run_id": run_id,
        "status": status,
        "documents": len(rows),
        "input_missing": len(missing),
    }
