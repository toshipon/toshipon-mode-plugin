"""The raw pages a collect run stored, checked against the hashes in `collect.json`.

Every later stage reads raw pages through this class, so a page changed after collect is
refused in one place.
"""

import re

from .. import paths, records
from ..errors import InputError

RAW_FILE = re.compile(r"[0-9]{4,}\.json")


class RawPages:
    """The pages listed in collect.json, grouped by what they answer."""

    def __init__(self, run_dir, collect):
        self.rest = {}  # (endpoint, number) -> [(file, items)]
        self.graphql = []  # (file, request, body)
        self.sha = {}
        for page in collect["pages"]:
            name = page["file"]
            if not RAW_FILE.fullmatch(name):
                raise InputError(f"collect.json lists a raw file with an unexpected name: {name!r}")
            path = paths.child(run_dir, "raw", name)
            text = path.read_text(encoding="utf-8") if path.is_file() else None
            if text is None or records.sha256(text) != page["sha256"]:
                raise InputError(f"raw/{name} is missing or changed after collect.")
            stored = records.read_json(path)
            request = stored["request"]
            self.sha[name] = page["sha256"]
            if request["kind"] == "rest":
                key = (request["endpoint"], request.get("number"))
                self.rest.setdefault(key, []).append((name, stored["body"]))
            else:
                self.graphql.append((name, request, stored["body"]))

    def items(self, endpoint, number=None):
        for name, items in self.rest.get((endpoint, number), []):
            for item in items:
                if isinstance(item, dict):
                    yield name, item
