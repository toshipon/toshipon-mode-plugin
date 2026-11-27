"""The claim contract: every statement carries a status, the exact source and revision it
rests on, and what is still uncertain. `verify` checks each excerpt against that source."""

import re

from .base import Refusal
from .gitread import REVISION, checked_path

# confirmed: a cited source says it.   inferred: a reading of cited sources, not written anywhere.
# conflict: cited sources disagree.     unknown: looked for and not found.
STATUSES = ("confirmed", "inferred", "conflict", "unknown")

_GIT = re.compile(r"^git:(?P<path>[^#]+)(?:#L(?P<start>[0-9]+)(?:-L(?P<end>[0-9]+))?)?$")
_PR = re.compile(r"^pr:(?P<number>[0-9]+)#(?:comment-(?P<comment>[0-9]+)|description)$")
_DOC = re.compile(
    r"^doc:(?P<source>[a-z][a-z0-9_-]{0,31}):(?P<id>[A-Za-z0-9][A-Za-z0-9_./-]{0,199})$"
)
_DOC_REVISION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:+-]{0,79}$")
_CLAIM_ID = re.compile(r"^C[0-9]{1,4}$")
MIN_EXCERPT, MAX_EXCERPT = 6, 600  # a shorter excerpt matches almost any source


def parse_locator(locator):
    """{"kind": "git" | "pr" | "doc", ...}. Raises Refusal for anything else."""
    if isinstance(locator, str):
        match = _GIT.match(locator)
        if match:
            start = int(match["start"]) if match["start"] else None
            end = int(match["end"]) if match["end"] else start
            if start is not None and not 1 <= start <= (end or start):
                raise Refusal(f"line range is empty: {locator}")
            return {"kind": "git", "path": checked_path(match["path"]), "start": start, "end": end}
        match = _PR.match(locator)
        if match:
            return {"kind": "pr", "number": int(match["number"]), "comment": match["comment"]}
        match = _DOC.match(locator)
        if match:
            return {"kind": "doc", "source": match["source"], "id": match["id"]}
    raise Refusal(
        f"locator must be git:<path>[#L<a>[-L<b>]], pr:<n>#comment-<id>, pr:<n>#description"
        f" or doc:<source>:<id>: {locator!r}"
    )


def check_reference(reference):
    """Problems with one {locator, revision, excerpt?}. Empty when it is well formed."""
    if not isinstance(reference, dict):
        return ["reference must be an object"]
    problems = []
    try:
        kind = parse_locator(reference.get("locator"))["kind"]
    except Refusal as refused:
        return [str(refused)]
    revision = reference.get("revision")
    pattern = _DOC_REVISION if kind == "doc" else REVISION
    if not isinstance(revision, str) or not pattern.match(revision):
        expected = "a version label" if kind == "doc" else "a commit sha (7-40 hex)"
        problems.append(f"{reference['locator']}: revision must be {expected}")
    excerpt = reference.get("excerpt")
    if excerpt is not None and (
        not isinstance(excerpt, str) or not MIN_EXCERPT <= len(_squash(excerpt)) <= MAX_EXCERPT
    ):
        problems.append(
            f"{reference['locator']}: excerpt must be {MIN_EXCERPT}-{MAX_EXCERPT} characters"
        )
    return problems


def _text(claim, key):
    value = claim.get(key)
    return value.strip() if isinstance(value, str) else ""


def check_claim(claim):
    """Problems with the shape of one claim. A claim that asserts something names its source;
    one that does not know says where it looked."""
    if not isinstance(claim, dict):
        return ["claim must be an object"]
    name = claim.get("id")
    if not isinstance(name, str) or not _CLAIM_ID.match(name):
        return [f"claim id must look like C1: {name!r}"]
    problems = []
    status = claim.get("status")
    if status not in STATUSES:
        return [f"{name}: status must be one of {', '.join(STATUSES)}"]
    if not _text(claim, "text"):
        problems.append(f"{name}: text is empty")
    references = claim.get("references", [])
    if not isinstance(references, list):
        return [f"{name}: references must be a list"]
    for reference in references:
        problems += [f"{name}: {problem}" for problem in check_reference(reference)]
    quoted = [ref for ref in references if isinstance(ref, dict) and ref.get("excerpt")]
    if status == "unknown":
        searched = claim.get("searched")
        if (
            not isinstance(searched, list)
            or not all(isinstance(s, str) and s.strip() for s in searched)
            or not searched
        ):
            problems.append(f"{name}: unknown must list what was searched")
        return problems
    if not references:
        problems.append(f"{name}: {status} needs a source locator and revision")
    if status == "confirmed" and not quoted:
        problems.append(f"{name}: confirmed needs a supporting excerpt")
    if status == "conflict" and len(quoted) < 2:
        problems.append(f"{name}: conflict needs an excerpt from each of the sources that disagree")
    if status in ("inferred", "conflict") and not _text(claim, "uncertainty"):
        problems.append(f"{name}: {status} must say what is uncertain")
    return problems


def check_claims(claims):
    if not isinstance(claims, list):
        return ["claims must be a list"]
    problems = [problem for claim in claims for problem in check_claim(claim)]
    names = [c["id"] for c in claims if isinstance(c, dict) and isinstance(c.get("id"), str)]
    problems += [
        f"{name}: duplicate claim id" for name in sorted({n for n in names if names.count(n) > 1})
    ]
    return problems


def _squash(text):
    return " ".join(text.split())


def cited_text(text, where):
    """The lines a git locator names, or the whole text. None when the range is not in the file."""
    if where["kind"] != "git" or where["start"] is None:
        return text
    lines = text.splitlines()
    if where["end"] > len(lines):
        return None
    return "\n".join(lines[where["start"] - 1 : where["end"]])


def verify_claim(claim, resolve):
    """Problems found when each reference is read back. `resolve(locator, revision)` returns
    the source text at that revision, or None when it cannot be read."""
    problems = []
    for reference in claim.get("references", []):
        locator, revision = reference["locator"], reference["revision"]
        text = resolve(locator, revision)
        if text is None:
            problems.append(f"{claim['id']}: source_unavailable: {locator}@{revision}")
            continue
        cited = cited_text(text, parse_locator(locator))
        if cited is None:
            problems.append(f"{claim['id']}: lines_out_of_range: {locator}@{revision}")
        elif reference.get("excerpt") and _squash(reference["excerpt"]) not in _squash(cited):
            problems.append(f"{claim['id']}: excerpt_not_found: {locator}@{revision}")
    return problems


def verify(claims, resolve):
    """Shape and evidence of every claim. A claim is unsupported when it asserts something
    (any status but unknown) and its shape or one of its references does not hold."""
    shape = check_claims(claims)
    if shape:
        raise Refusal("claims do not follow the contract", shape)
    results = []
    for claim in claims:
        problems = verify_claim(claim, resolve)
        results.append(
            {
                "id": claim["id"],
                "status": claim["status"],
                "supported": not problems,
                "problems": problems,
            }
        )
    asserting = [result for result in results if result["status"] != "unknown"]
    unsupported = [result for result in asserting if not result["supported"]]
    return {
        "claims": len(results),
        "asserting": len(asserting),
        "unsupported": len(unsupported),
        "unsupported_claim_rate": round(len(unsupported) / len(asserting), 4)
        if asserting
        else None,
        "by_status": {status: sum(r["status"] == status for r in results) for status in STATUSES},
        "results": results,
    }
