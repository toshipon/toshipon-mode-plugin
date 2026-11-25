"""Synthetic GitHub responses read from disk, behind the same interface as `GhTransport`.

A fixture directory holds `index.json` and one file per response. Each response file is the
text `gh api -i` would print (status line, headers, empty line, body), so it goes through the
same parser as the real transport. `index.json` maps a request key to a list of file names;
repeated requests take the next file, and the last one repeats.

Keys are built by the same guard functions that check real requests, so a fixture cannot
answer a request the guard would refuse. Nothing here starts a process or opens a socket.
"""

import json
import re
from pathlib import Path

from .. import guard
from ..errors import InputError
from .rest import MAX_RESPONSE_BYTES, ResponseTooLarge, parse_response

INDEX = "index.json"
_FILE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\.http")


def rest_key(endpoint, values, query):
    return guard.rest_path(endpoint, values, query)


def graphql_key(name, variables):
    guard.graphql_query(name)
    arguments = guard.graphql_fields(variables)
    pairs = [arguments[index + 1] for index in range(0, len(arguments), 2)]
    return f"graphql:{name}?" + "&".join(pairs)


class FixtureTransport:
    def __init__(self, directory):
        self.directory = Path(directory)
        index = self.directory / INDEX
        if not self.directory.is_dir() or not index.is_file():
            raise InputError(f"--fixture must be a directory with {INDEX}: {self.directory}")
        try:
            responses = json.loads(index.read_text(encoding="utf-8"))["responses"]
        except (ValueError, KeyError, TypeError):
            raise InputError(f"{index} is not a fixture index.") from None
        if not isinstance(responses, dict) or not all(
            isinstance(files, list) and files and all(_FILE.fullmatch(str(f)) for f in files)
            for files in responses.values()
        ):
            raise InputError(f"{index}: every key must map to a list of response file names.")
        self._responses = responses
        self._served = {}
        self.requests = []

    def __repr__(self):
        return f"FixtureTransport({str(self.directory)!r})"

    def answer(self, key):
        """The stored `gh api -i` text for a request key. Each call takes the next file."""
        self.requests.append(key)
        files = self._responses.get(key)
        if files is None:
            raise InputError(f"the fixture has no response for {key}")
        count = self._served.get(key, 0)
        self._served[key] = count + 1
        path = self.directory / files[min(count, len(files) - 1)]
        if path.is_symlink() or not path.is_file():
            raise InputError(f"fixture response file is missing: {path.name}")
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            raise ResponseTooLarge(
                f"a response was larger than {MAX_RESPONSE_BYTES} bytes; "
                "collection stopped instead of truncating it."
            )
        return path.read_text(encoding="utf-8")

    def rest(self, endpoint, values, query):
        return parse_response(self.answer(rest_key(endpoint, values, query)))

    def graphql(self, name, variables):
        return parse_response(self.answer(graphql_key(name, variables)))
