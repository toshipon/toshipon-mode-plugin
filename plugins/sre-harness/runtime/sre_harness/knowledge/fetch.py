"""`knowledge fetch`: the source text behind one record's evidence, a page at a time.

No model, no network, no cache. The steps:

    1. resolve the principal (unknown or no config: exit 3) and stop with stale_index when the
       index was built with another knowledge/config.json
    2. read the record through the store's access filter. A record the principal cannot read,
       one that never existed and one that left the index (a tombstone) all get the same
       answer, so the answer says nothing about which it was
    3. a retired or rejected record is answered as withdrawn, without text
    4. read each evidence locator (`sources.read`) and keep a text only when its sha256 is the
       recorded one (else hash_mismatch) and it carries no secret shape and no denied term
       (else secret_shape; `withheld` names the shapes, never the text)
    5. page the kept texts: whole lines only, inside the untrusted envelope, within
       --max-tokens; `next_offset` is where the next page starts

Rules this module decides:

    lines        a line ends at "\\n" (as records.read_jsonl reads); the last one may not
    offset       counts lines across the record's fetched evidence, in evidence order
    tokens       what a fragment costs is terms.estimate_tokens of its envelope, the text the
                 reader gets; a page holds as many whole lines as fit
    too small    when not even the next line fits, the answer is max_tokens_too_small with
                 the tokens that line needs; nothing is cut
    record       the status, enforcement, kind and content hash are the index's. Nothing in
                 a source text can change them: fetch writes nothing to the index
"""

import math
import re
from dataclasses import dataclass

from .. import privacy, records
from ..errors import EXIT_OK, EXIT_QUALITY, BoundaryError, InputError
from ..review.markdown import clean
from . import config, sources, store, terms, untrusted

DEFAULT_MAX_TOKENS = 3000
WITHDRAWN = frozenset({"retired", "rejected"})
DENY_TERM = "deny_term"
_LINE = re.compile(r"[^\n]*\n|[^\n]+\Z")


@dataclass(frozen=True)
class Request:
    record_id: str
    max_tokens: int = DEFAULT_MAX_TOKENS
    offset: int = 0
    generation: int | None = None
    deny_terms: tuple = ()


def split_lines(text):
    """Lines with their "\\n"; joined again they are the text."""
    return _LINE.findall(text)


def withheld_shapes(text, deny_terms):
    """Names of what keeps a text back: secret shapes and `deny_term`, looked for in the text
    as it was read and as it is printed (without invisible characters, so a zero-width space
    cannot hide either). Never the text itself."""
    examined = text + "\n" + untrusted.clean(text)
    shapes = privacy.find_secrets(examined)
    if privacy.find_deny_terms(examined, deny_terms):
        shapes.append(DENY_TERM)
    return shapes


def _pointer(evidence):
    return {"locator": clean(evidence["locator"]), "revision": clean(evidence["revision"])}


def examine(record, reads, deny_terms=()):
    """(kept [(evidence, lines)], unfetched, withheld) for `record`'s evidence and what
    `sources.read` returned for each. Pure."""
    kept, unfetched, withheld = [], [], []
    for evidence, found in zip(record["evidence"], reads):
        pointer = _pointer(evidence)
        if found.text is None:
            unfetched.append({**pointer, "reason": found.reason})
            continue
        digest = records.sha256(found.text)
        if digest != evidence["content_sha256"]:
            unfetched.append(
                {
                    **pointer,
                    "reason": "hash_mismatch",
                    "hash_match": False,
                    "content_sha256": digest,
                }
            )
            continue
        shapes = withheld_shapes(found.text, deny_terms)
        if shapes:
            unfetched.append({**pointer, "reason": "secret_shape"})
            withheld.append({"locator": pointer["locator"], "shapes": shapes})
            continue
        kept.append((evidence, split_lines(found.text)))
    return kept, unfetched, withheld


def _label(evidence):
    return f"{evidence['locator']}@{evidence['revision']}"


def _bytes(line):
    return len(untrusted.clean(line).encode("utf-8"))


def page(kept, offset, max_tokens):
    """(fragments, next offset or None, tokens the next line needs when none fit, else None).

    Lines are taken in order from `offset` while the envelope they make stays within
    `max_tokens`: the envelope's own bytes are counted at their most (with the flag), so the
    fragment's real cost is never above what was checked."""
    total = sum(len(lines) for _, lines in kept)
    if offset and offset >= total:
        raise InputError(f"--offset {offset} is past the end: the sources have {total} lines.")
    fragments, left, position = [], max_tokens, 0
    for evidence, lines in kept:
        start, position = position, position + len(lines)
        if offset >= position:
            continue
        first = max(offset - start, 0)
        label = _label(evidence)
        size, taken = untrusted.overhead_bytes(label), first
        while taken < len(lines) and math.ceil((size + _bytes(lines[taken])) / 3) <= left:
            size += _bytes(lines[taken])
            taken += 1
        if taken == first:
            if fragments:
                return fragments, start + first, None
            needed = math.ceil((untrusted.overhead_bytes(label) + _bytes(lines[first])) / 3)
            return [], start + first, needed
        text = untrusted.envelope(label, "".join(lines[first:taken]))
        left -= terms.estimate_tokens(text)
        pointer = _pointer(evidence)
        fragments.append(
            {
                **pointer,
                "content_sha256": evidence["content_sha256"],
                "hash_match": True,
                "size": evidence["size"],
                "start_line": first + 1,
                "end_line": taken,
                "total_lines": len(lines),
                "instruction_like": text.split("\n", 1)[0].endswith(untrusted.FLAG + ">>>"),
                "text": text,
            }
        )
        if taken < len(lines):
            return fragments, start + taken, None
    return fragments, None, None


def _budget(request, used, needed=None):
    value = {
        "estimator": terms.ESTIMATOR,
        "max_tokens": request.max_tokens,
        "used_tokens": used,
        "task": None,
    }
    if needed is not None:
        value["needed_tokens"] = needed
    return value


def answer(head, request, status, **parts):
    """One fetch result. `head` holds schema_version, snapshot, principal and id."""
    fragments = parts.get("fragments", [])
    return {
        **head,
        "status": status,
        "record": parts.get("record"),
        "budget": _budget(
            request, sum(terms.estimate_tokens(f["text"]) for f in fragments), parts.get("needed")
        ),
        "offset": request.offset,
        "next_offset": parts.get("next_offset"),
        "fragments": fragments,
        "unfetched": parts.get("unfetched", []),
        "withheld": parts.get("withheld", []),
    }


def exhausted(result, task_view):
    """The result once the task total refused it: no text, the rest of the answer kept."""
    return {
        **result,
        "status": "task_budget_exhausted",
        "budget": {**result["budget"], "used_tokens": 0, "task": task_view},
        "next_offset": result["offset"],
        "fragments": [],
    }


def _record(row):
    return {name: row[name] for name in ("status", "enforcement", "kind", "content_hash")}


def run(root, name, request, read=sources.read):
    settings = config.load(root)
    if settings is None:
        raise BoundaryError("no knowledge/config.json, so no principal can be resolved.")
    principal = config.principal(settings, name)
    with store.reader(root) as reader:
        meta = reader.meta()
        head = {
            "schema_version": records.SCHEMA_VERSION,
            "snapshot": {
                "generation": reader.generation,
                "updated_at": meta["updated_at"],
                "asked_generation": request.generation,
                "moved": request.generation is not None and request.generation != reader.generation,
            },
            "principal": principal.name,
            "id": request.record_id,
        }
        if meta["config_hash"] != config.config_hash(settings):
            return answer(head, request, "stale_index")
        found = reader.records(principal, ids=[request.record_id])
    if not found:
        return answer(head, request, "incomplete", unfetched=[{"reason": "access_denied"}])
    (row,) = found
    if row["status"] in WITHDRAWN:
        return answer(head, request, "withdrawn", record=_record(row))
    reads = [read(root, row, evidence) for evidence in row["evidence"]]
    kept, unfetched, withheld = examine(row, reads, request.deny_terms)
    fragments, next_offset, needed = page(kept, request.offset, request.max_tokens)
    if needed is not None:
        status = "max_tokens_too_small"
    else:
        status = "incomplete" if unfetched else "ok"
    return answer(
        head,
        request,
        status,
        record=_record(row),
        fragments=fragments,
        next_offset=next_offset,
        unfetched=unfetched,
        withheld=withheld,
        needed=needed,
    )


def exit_code(result):
    return EXIT_OK if result["status"] == "ok" else EXIT_QUALITY
