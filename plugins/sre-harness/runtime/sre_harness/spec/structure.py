"""Fixed rules that read a file of the repository into units, references and facts.

Nothing here guesses. A file is read only when its path says what it is:

- `*.tf` is IaC. A unit is one top-level `resource "type" "name" {`, `data "type" "name" {` or
  `module "name" {` block, with the comment lines right above it. Its node is the resource
  address (`aws_s3_bucket.logs`, `data.type.name`, `module.name`). IDs in those comments are
  references. An attribute at the block's top level with an ID in its line-end comment
  (`retention_days = 400 # REQ-0001`) is a fact of that ID.
- A test file (under `tests/`, `test/`, `spec/`, or named `test_*.py`, `*_test.py`,
  `*_test.go`, `*.test.js`, ...) has one unit per test (`def test_...`, `func Test...`,
  `it("...")`, `test("...")`). An ID in the test's name (`test_req_0001_...`) is a reference.
- Markdown (`*.md`) has one unit per section (ATX headings, as `spec ingest` splits them) and
  one for the front-matter. A section whose heading starts with an ID defines that entity, and
  so do its subsections; a document whose front-matter has `id: ADR-0006` defines it as a
  whole. Every other section belongs to the document itself (a `design` node). IDs anywhere
  else in a section are references. `- key: value` bullets are facts of the ID in the nearest
  heading that names exactly one ID, or of the front-matter `id`. Front-matter keys other than
  the reserved ones are facts too.

An ID is `REQ-0001` (four digits or more), `ADR-1` to `ADR-999999` or `DEC-0001`. A fact is
(entity, key) -> value with the value as written (quotes removed). Two facts are the same only
when the entity and the key are the same; the IaC attribute and the document bullet must use
the same key name.
"""

import re

from . import ingest_file

ID = re.compile(r"(?<![A-Za-z0-9_])(?:REQ-[0-9]{4,}|(?:ADR|DEC)-[0-9]{1,6})(?![0-9])")
_TEST_ID = re.compile(r"(?<![A-Za-z])(req|adr|dec)[-_]?([0-9]{4,})(?![0-9])", re.IGNORECASE)
_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|spec)/[^/]+\.(?:py|go|js|ts|jsx|tsx|rb)$"
    r"|(?:^|/)test_[^/]*\.py$|_test\.(?:py|go)$|\.(?:test|spec)\.[jt]sx?$"
)
_TEST_DEF = re.compile(r"[ \t]*(?:async[ \t]+)?(?:def|func)[ \t]+([Tt]est[A-Za-z0-9_]{0,200})\b.*")
_TEST_CALL = re.compile(r"""[ \t]*(?:it|test)\([ \t]*(['"])(.{1,200}?)\1.*""")
_BLOCK = re.compile(
    r'[ \t]*(resource|data|module)[ \t]+"([A-Za-z0-9_-]{1,100})"'
    r'(?:[ \t]+"([A-Za-z0-9_-]{1,100})")?[ \t]*\{.*'
)
_ATTRIBUTE = re.compile(r"[ \t]*([a-z][a-z0-9_]{0,63})[ \t]*=[ \t]*([^#]*?)[ \t]*(?:#(.*))?")
_COMMENT = re.compile(r"[ \t]*(?:#|//)(.*)")
_FACT = re.compile(r"[ \t]*[-*][ \t]+([a-z][a-z0-9_]{0,63})[ \t]*:[ \t]+(\S.*?)[ \t]*")
NOT_FACT_KEYS = {"given", "when", "then", "and"}
RESERVED_FRONT_MATTER = {
    "id",
    "title",
    "status",
    "date",
    "supersedes",
    "approvers",
    "systems",
    "components",
    "environments",
    "attendees",
    "tags",
}
MAX_QUOTE = 200


def file_kind(path):
    """`iac`, `test`, `markdown`, or None for a file these rules do not read."""
    if path.endswith(".tf"):
        return "iac"
    if _TEST_PATH.search(path):
        return "test"
    if path.endswith((".md", ".markdown")):
        return "markdown"
    return None


def id_type(written):
    return "requirement" if written.startswith("REQ-") else "decision"


def _value(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def _unit(node, start, end):
    return {"node": node, "start": start, "end": end, "refs": [], "facts": [], "text": ""}


def _ref(written, line, quote, hint=None):
    return {"id": written, "line": line, "quote": quote.strip()[:MAX_QUOTE], "hint": hint}


# Markdown ---------------------------------------------------------------------------------


def _front_matter_unit(path, parsed, doc_node, doc_id):
    lines = parsed["raw"].splitlines()
    unit = _unit(doc_node, 1, len(lines))
    unit["text"] = parsed["raw"]
    for number, line in enumerate(lines, start=1):
        key = line.split(":", 1)[0].strip().lower() if ":" in line else None
        hint = "supersedes" if key == "supersedes" else None
        if key == "id":
            continue
        for written in ID.findall(line):
            if written != doc_id:
                unit["refs"].append(_ref(written, number, line, hint))
    for key, value in parsed["fields"].items():
        if key in RESERVED_FRONT_MATTER or not isinstance(value, str) or not value or not doc_id:
            continue
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", key):
            continue
        number = next(
            (i for i, line in enumerate(lines, 1) if line.lower().startswith(f"{key}:")), 1
        )
        unit["facts"].append((doc_id, key, value, number))
    return unit


def parse_markdown(path, text):
    parsed = ingest_file.front_matter(text)
    stated = parsed["fields"].get("id")
    doc_id = stated if isinstance(stated, str) and ID.fullmatch(stated) else None
    doc_node = (id_type(doc_id), doc_id) if doc_id else ("design", path)
    lines = text.splitlines()
    units = []
    if parsed["raw"] is not None:
        units.append(_front_matter_unit(path, parsed, doc_node, doc_id))
    stack = []  # (level, ids in the heading, the heading starts with an id)
    for section in ingest_file.markdown_sections(parsed["body"], parsed["body_line"]):
        level, heading = section["level"], section["heading"]
        while stack and stack[-1][0] >= level:
            stack.pop()
        ids = ID.findall(heading)
        if level:
            stack.append((level, ids, bool(ID.match(heading.strip()))))
        defining = next((entry[1][0] for entry in reversed(stack) if entry[2]), None)
        single = next((entry[1][0] for entry in reversed(stack) if len(entry[1]) == 1), None)
        node = (id_type(defining), defining) if defining else doc_node
        fact_entity = single or doc_id
        start, end = (int(part) for part in section["locator"][1:].split("-L"))
        unit = _unit(node, start, end)
        unit["text"] = "\n".join(lines[start - 1 : end])
        for number in range(start, end + 1):
            line = lines[number - 1] if number <= len(lines) else ""
            for written in ID.findall(line):
                if written != node[1]:
                    unit["refs"].append(_ref(written, number, line))
            fact = _FACT.fullmatch(line)
            if fact and fact_entity and fact.group(1) not in NOT_FACT_KEYS:
                unit["facts"].append((fact_entity, fact.group(1), _value(fact.group(2)), number))
        units.append(unit)
    return units


# IaC --------------------------------------------------------------------------------------


def _address(kind, first, second):
    if kind == "module":
        return f"module.{first}"
    if second is None:
        return None
    return f"data.{first}.{second}" if kind == "data" else f"{first}.{second}"


def _depth(line):
    return line.count("{") - line.count("}")


def _comment_ids(line):
    for marker in ("#", "//"):
        if marker in line:
            return ID.findall(line.split(marker, 1)[1])
    return []


def parse_tf(path, text):
    lines = text.splitlines()
    units, depth, current, comments = [], 0, None, []
    for number, line in enumerate(lines, start=1):
        if current is None:
            block = _BLOCK.fullmatch(line) if depth == 0 else None
            address = _address(*block.groups()) if block else None
            if address is None:
                comments = comments + [number] if depth == 0 and _COMMENT.fullmatch(line) else []
                depth = max(0, depth + _depth(line))
                continue
            current = _unit(("iac", address), comments[0] if comments else number, number)
            for at in [*comments, number]:
                for written in _comment_ids(lines[at - 1]):
                    current["refs"].append(_ref(written, at, lines[at - 1]))
            comments, depth = [], _depth(line)
        else:
            attribute = _ATTRIBUTE.fullmatch(line) if depth == 1 else None
            for written in _comment_ids(line):
                current["refs"].append(_ref(written, number, line))
                if attribute:
                    value = _value(attribute.group(2))
                    current["facts"].append((written, attribute.group(1), value, number))
            depth += _depth(line)
        current["end"] = number
        if depth <= 0:
            depth = 0
            current["text"] = "\n".join(lines[current["start"] - 1 : number])
            units.append(current)
            current = None
    if current is not None:
        current["text"] = "\n".join(lines[current["start"] - 1 :])
        units.append(current)
    return units


# Tests ------------------------------------------------------------------------------------


def parse_tests(path, text):
    lines = text.splitlines()
    starts = []
    for number, line in enumerate(lines, start=1):
        match = _TEST_DEF.fullmatch(line) or _TEST_CALL.fullmatch(line)
        if match:
            name = match.group(1) if match.re is _TEST_DEF else match.group(2)
            starts.append((number, name))
    units = []
    for index, (number, name) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        unit = _unit(("test", f"{path}::{name}"), number, end)
        unit["text"] = "\n".join(lines[number - 1 : end])
        for kind, digits in _TEST_ID.findall(name):
            unit["refs"].append(_ref(f"{kind.upper()}-{digits}", number, name, "test_name"))
        units.append(unit)
    return units


PARSERS = {"markdown": parse_markdown, "iac": parse_tf, "test": parse_tests}


def parse(path, text):
    """Units of one file version, or [] for a file these rules do not read."""
    kind = file_kind(path)
    return PARSERS[kind](path, text) if kind else []


def bullet_facts(text):
    """[(key, value)] of the `- key: value` bullets in a text."""
    found = []
    for line in text.splitlines():
        match = _FACT.fullmatch(line)
        if match and match.group(1) not in NOT_FACT_KEYS:
            found.append((match.group(1), _value(match.group(2))))
    return found


def facts(units):
    """{(entity, key): (value, line)} of a file version. The first statement of a key wins;
    a second, different value in the same file is returned in the conflicts list."""
    found, conflicts = {}, []
    for unit in units:
        for entity, key, value, line in unit["facts"]:
            if (entity, key) in found and found[(entity, key)][0] != value:
                conflicts.append((entity, key, value, line))
                continue
            found.setdefault((entity, key), (value, line))
    return found, conflicts


def touched(units, ranges):
    """Units with at least one line in `ranges` (inclusive (start, end) pairs)."""
    return [
        unit
        for unit in units
        if any(start <= unit["end"] and unit["start"] <= end for start, end in ranges)
    ]
