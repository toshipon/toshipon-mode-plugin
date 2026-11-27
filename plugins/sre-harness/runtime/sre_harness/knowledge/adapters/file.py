"""The `file` adapter: a JSON file of items (knowledge-record `$defs/source_file`).

It reads the whole file every time and says whether that file is complete; the store works out
what changed. The cursor is the sha256 of the file, kept so that `status` shows which input
the index was last built from.
"""

import json
from pathlib import Path

from ... import records
from ...errors import InputError

MAX_BYTES = 32 * 1024 * 1024


class FileAdapter:
    name = "file"

    def __init__(self, path):
        self.path = Path(path)
        self._loaded = None

    @classmethod
    def from_options(cls, options):
        from . import refuse_run_options

        refuse_run_options(cls.name, options)
        if not options.get("input"):
            raise InputError("--source file needs --input <path to a JSON file>.")
        return cls(options["input"])

    def _load(self):
        if self._loaded is None:
            try:
                data = self.path.read_bytes()
            except FileNotFoundError:
                raise InputError(f"missing file: {self.path}") from None
            except OSError as error:
                raise InputError(f"cannot read {self.path}: {error.strerror}") from None
            if len(data) > MAX_BYTES:
                raise InputError(f"{self.path} is larger than {MAX_BYTES} bytes.")
            try:
                value = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise InputError(f"{self.path} is not valid JSON: {error}") from None
            problems = records.violations("knowledge-record", value, "source_file")
            if problems:
                raise InputError(
                    f"{self.path} is not a knowledge source file: " + "; ".join(problems[:5])
                )
            self._loaded = (value, "sha256:" + records.sha256(data))
        return self._loaded

    @property
    def source(self):
        return f"file:{self._load()[0]['source']}"

    def changes(self, cursor):
        from . import Changes

        value, digest = self._load()
        return Changes(
            items=value["items"],
            tombstones=value.get("tombstones", []),
            cursor=digest,
            complete=value["complete"],
        )
