"""Where a locator is read back from. Git locators are read from the repository at the cited
revision. Everything else (PR comments, documents fetched in the agent session) is read from
a snapshot the session saved here, secrets replaced before it touches the disk."""

import json
from pathlib import Path

from .base import Refusal, now, read_json, sha256, write_private
from .claims import check_reference, parse_locator
from .gitread import resolve as resolve_revision
from .gitread import show
from .redaction import redact_text

MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024


class Evidence:
    def __init__(self, root, directory):
        self.root = Path(root)
        self.directory = Path(directory)

    def _path(self, locator, revision):
        return self.directory / f"{sha256(f'{locator}@{revision}')}.json"

    def add(self, locator, revision, text):
        """Save a snapshot of a source that git cannot read back."""
        problems = check_reference({"locator": locator, "revision": revision})
        if problems:
            raise Refusal("snapshot needs a locator and a revision", problems)
        if parse_locator(locator)["kind"] == "git":
            raise Refusal("git sources are read from the repository, not from a snapshot")
        if len(text.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise Refusal(f"snapshot is larger than {MAX_SNAPSHOT_BYTES} bytes: {locator}")
        text = redact_text(text)
        record = {
            "locator": locator,
            "revision": revision,
            "sha256": sha256(text),
            "size": len(text.encode("utf-8")),
            "retrieved_at": now(),
            "text": text,
        }
        write_private(self._path(locator, revision), json.dumps(record, ensure_ascii=False))
        return {
            key: record[key] for key in ("locator", "revision", "sha256", "size", "retrieved_at")
        }

    def resolve(self, locator, revision):
        """The source text at the revision, or None when it cannot be read."""
        where = parse_locator(locator)
        if where["kind"] == "git":
            if resolve_revision(self.root, revision) is None:
                return None
            return show(self.root, revision, where["path"])
        path = self._path(locator, revision)
        return read_json(path)["text"] if path.exists() else None
