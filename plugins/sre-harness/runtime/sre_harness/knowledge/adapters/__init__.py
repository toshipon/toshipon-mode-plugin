"""Source adapters: where knowledge comes from.

An adapter has a `name` (the `--source` value), a `source` (a stable name for the one source it
reads, which keys its cursor in the index), and `changes(cursor)`, which returns `Changes`:

    items       knowledge-record `$defs/item` values, checked one by one by the update
    tombstones  {origin, reason} of records the source says are gone
    cursor      what to hand back next time (the index keeps it per source)
    complete    True when `items` is all the source holds. Only then does a record missing
                from it become a tombstone; a partial answer never deletes.
"""

from dataclasses import dataclass

from ...errors import InputError
from . import file


@dataclass(frozen=True)
class Changes:
    items: list
    tombstones: list
    cursor: str | None
    complete: bool


ADAPTERS = {"file": file.FileAdapter}


def build(name, options):
    """The adapter `name`, built from the command-line options it needs."""
    factory = ADAPTERS.get(name)
    if factory is None:
        raise InputError(f"unknown source {name!r}; known: {', '.join(sorted(ADAPTERS))}.")
    return factory.from_options(options)
