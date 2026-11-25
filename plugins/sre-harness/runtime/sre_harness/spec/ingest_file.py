"""Files to documents: Markdown, ADRs, minutes, and Slack and Confluence *exports* (no API).

Reading rules, all enforced here:

- Paths come from the manifest and are relative to its directory. An absolute path, a `..`
  component or a backslash is refused (exit 3) before anything is read.
- No symlink is followed: every directory on the way and the file itself are checked with
  `lstat`, and the file is opened with `O_NOFOLLOW`. A symlink is refused (exit 3).
- Files are only read. A file over `MAX_DOCUMENT_BYTES`, or a run whose documents together
  pass `MAX_RUN_BYTES`, stops the stage (exit 1) instead of being cut.
- A listed file that is absent, not a regular file, not UTF-8, or an export of another shape
  is recorded as `input_missing`; nothing is guessed in its place.

Front-matter is read without a YAML library: `key: value`, `key: [a, b]` and a `key:` followed
by `- item` lines. Anything else (nested mappings, block scalars, flow mappings) is listed in
the document's `missing` as `front_matter.<key>` and left out.
"""

import json
import os
import re
import stat
from html.parser import HTMLParser
from pathlib import PurePosixPath

from .. import records
from ..errors import BoundaryError, QualityGateError

MAX_DOCUMENT_BYTES = 2 * 1024 * 1024
MAX_RUN_BYTES = 64 * 1024 * 1024
TEXT_KINDS = ("markdown", "adr", "minutes")
FRONT_MATTER_HEADING = "front-matter"

_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_-]{0,63})[ \t]*:(.*)")
_ITEM = re.compile(r"[ \t]*-[ \t]+(.*)")
_HEADING = re.compile(r"(#{1,6})[ \t]+(.*?)(?:[ \t]+#+)?[ \t]*")
_FENCE = re.compile(r"[ \t]{0,3}(```|~~~)")
_SLACK_TS = re.compile(r"[0-9]{1,12}\.[0-9]{1,8}")


class Unreadable(Exception):
    """The file is there but cannot become a document. Carries the `input_missing` reason."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


# Paths ------------------------------------------------------------------------------------


def relative_parts(text, what="path"):
    """The components of a manifest path, refusing anything that could leave the base."""
    if not isinstance(text, str) or not text or "\\" in text or "\0" in text:
        raise BoundaryError(f"refused manifest {what} {text!r}: not a plain relative path.")
    path = PurePosixPath(text)
    parts = [part for part in path.parts if part not in ("", ".")]
    if path.is_absolute() or text.startswith("~") or not parts or ".." in parts:
        raise BoundaryError(
            f"refused manifest {what} {text!r}: it must stay inside the manifest's directory "
            "(no absolute path, no '..')."
        )
    return parts


def check_patch_glob(text):
    parts = relative_parts(text, "patch glob")
    if {".git", ".github"} & set(parts):
        raise BoundaryError(f"refused manifest patch glob {text!r}: .git/ and .github/ never.")
    return "/".join(parts)


def read_bytes(base, parts):
    """The file's bytes. Raises Unreadable for a gap, BoundaryError for a symlink."""
    current = base
    for index, part in enumerate(parts):
        current = current / part
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            raise Unreadable("file_not_found") from None
        if stat.S_ISLNK(info.st_mode):
            raise BoundaryError(f"refused to follow a symlink: {'/'.join(parts[: index + 1])}")
        last = index == len(parts) - 1
        if not last and not stat.S_ISDIR(info.st_mode):
            raise Unreadable("file_not_found")
    if not stat.S_ISREG(info.st_mode):
        raise Unreadable("not_a_regular_file")
    check_size(info.st_size, "/".join(parts))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        handle = os.open(current, flags)
    except OSError:
        raise BoundaryError(f"refused to open {'/'.join(parts)}: not a plain file.") from None
    with os.fdopen(handle, "rb") as stream:
        data = stream.read(MAX_DOCUMENT_BYTES + 1)
    check_size(len(data), "/".join(parts))
    return data


def check_size(size, name):
    if size > MAX_DOCUMENT_BYTES:
        raise QualityGateError(
            f"{name} is {size} bytes, over the {MAX_DOCUMENT_BYTES}-byte cap per document; "
            "stopped instead of truncating it."
        )


# Front-matter -----------------------------------------------------------------------------


def _scalar(text):
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        return text[1:-1]
    return text


def front_matter(text):
    """{fields, raw, body, body_line, missing}. `raw` is the block as written, delimiters
    included; `body_line` is the 1-based line where the body starts."""
    lines = text.splitlines(keepends=True)
    absent = {"fields": {}, "raw": None, "body": text, "body_line": 1, "missing": []}
    if not lines or lines[0].rstrip() != "---":
        return absent
    end = next(
        (i for i in range(1, len(lines)) if lines[i].rstrip() in ("---", "...")),
        None,
    )
    if end is None:
        return {**absent, "missing": ["front_matter"]}
    fields, missing, key, broken = {}, [], None, False
    for number, line in enumerate(lines[1:end], start=2):
        content = line.rstrip("\r\n")
        if not content.strip() or content.lstrip().startswith("#"):
            continue
        item = _ITEM.fullmatch(content)
        if content[0] in " \t" or item:
            if broken:
                continue
            if key is not None and item and isinstance(fields.get(key), list):
                fields[key].append(_scalar(item.group(1)))
                continue
            if key is not None:
                fields.pop(key, None)
                missing.append(f"front_matter.{key}")
                broken = True
                continue
            missing.append(f"front_matter.line_{number}")
            continue
        match = _KEY.fullmatch(content)
        if match is None:
            missing.append(f"front_matter.line_{number}")
            key, broken = None, True
            continue
        key, broken = match.group(1).lower(), False
        value = match.group(2).strip()
        if not value:
            fields[key] = []  # a list if items follow; settled below
        elif value[0] in "|>{":
            missing.append(f"front_matter.{key}")
            broken = True
        elif value[0] == "[" and value[-1] == "]":
            inner = value[1:-1].strip()
            fields[key] = [_scalar(part) for part in inner.split(",")] if inner else []
        else:
            fields[key] = _scalar(value)
    # `key:` with no items under it is an empty string, as written.
    explicit_lists = {
        match.group(1).lower()
        for line in lines[1:end]
        if (match := _KEY.fullmatch(line.rstrip("\r\n"))) and match.group(2).strip().startswith("[")
    }
    for name, value in list(fields.items()):
        if value == [] and name not in explicit_lists:
            fields[name] = ""
    return {
        "fields": fields,
        "raw": "".join(lines[: end + 1]),
        "body": "".join(lines[end + 1 :]),
        "body_line": end + 2,
        "missing": missing,
    }


# Sections ---------------------------------------------------------------------------------


def markdown_sections(text, first_line=1):
    """Split at ATX headings outside code fences. Each section keeps its line range."""
    sections, fenced = [], False
    current = {"heading": "", "level": 0, "start": first_line, "lines": []}
    for offset, line in enumerate(text.splitlines()):
        number = first_line + offset
        if _FENCE.match(line):
            fenced = not fenced
        heading = None if fenced else _HEADING.fullmatch(line)
        if heading:
            sections.append(current)
            current = {
                "heading": heading.group(2).strip(),
                "level": len(heading.group(1)),
                "start": number,
                "lines": [],
            }
        else:
            current["lines"].append(line)
        current["end"] = number
    sections.append(current)
    out = []
    for section in sections:
        body = "\n".join(section["lines"]).strip()
        if section["level"] == 0 and not body:
            continue
        end = section.get("end", section["start"])
        out.append(
            {
                "heading": section["heading"],
                "level": section["level"],
                "text": body,
                "locator": f"L{section['start']}-L{end}",
            }
        )
    return out


def slack_sections(value):
    """A Slack export day file: a JSON array of messages with `ts` and `text`."""
    if not isinstance(value, list):
        raise ValueError("a Slack export is a JSON array of messages")
    sections = []
    for message in value:
        if not isinstance(message, dict):
            raise ValueError("every Slack message is an object")
        ts, text = message.get("ts"), message.get("text")
        if not isinstance(ts, str) or not _SLACK_TS.fullmatch(ts) or not isinstance(text, str):
            raise ValueError("every Slack message has a string `ts` and `text`")
        author = message.get("user_name") or message.get("user") or ""
        locator = f"ts {ts}"
        thread = message.get("thread_ts")
        if isinstance(thread, str) and _SLACK_TS.fullmatch(thread) and thread != ts:
            locator += f" thread {thread}"
        sections.append({"heading": str(author)[:100], "text": text, "locator": locator})
    return sections


class _Storage(HTMLParser):
    """Confluence storage XHTML to text, split at h1-h6. Markup and attributes are dropped."""

    BLOCKS = {"p", "div", "li", "tr", "br", "pre", "blockquote", "table", "ul", "ol"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections = [{"heading": "", "parts": []}]
        self.heading = None
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif re.fullmatch(r"h[1-6]", tag):
            self.heading = []
        elif tag in self.BLOCKS:
            self.sections[-1]["parts"].append("\n- " if tag == "li" else "\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif re.fullmatch(r"h[1-6]", tag) and self.heading is not None:
            self.sections.append({"heading": "".join(self.heading).strip(), "parts": []})
            self.heading = None
        elif tag in self.BLOCKS:
            self.sections[-1]["parts"].append("\n")

    def handle_data(self, data):
        if self.skip:
            return
        if self.heading is not None:
            self.heading.append(data)
        else:
            self.sections[-1]["parts"].append(data)


def confluence_sections(value):
    """A saved Confluence page (`title`, `body.storage.value`). Returns (title, sections)."""
    try:
        title = value["title"]
        storage = value["body"]["storage"]["value"]
    except (KeyError, TypeError):
        raise ValueError("a Confluence export has `title` and `body.storage.value`") from None
    if not isinstance(title, str) or not isinstance(storage, str):
        raise ValueError("a Confluence export has string `title` and `body.storage.value`")
    parser = _Storage()
    parser.feed(storage)
    parser.close()
    sections = []
    for index, section in enumerate(parser.sections):
        lines = [" ".join(line.split()) for line in "".join(section["parts"]).split("\n")]
        text = "\n".join(line for line in lines if line)
        if index == 0 and not text:
            continue
        label = f"heading {index}" if index else "before the first heading"
        sections.append({"heading": section["heading"], "text": text, "locator": label})
    return title, sections


# Documents --------------------------------------------------------------------------------


def numbered(sections):
    return [
        {"id": f"S{index}", **{key: s[key] for key in ("heading", "text", "locator")}}
        for index, s in enumerate(sections, start=1)
    ]


def _text_document(entry, text, parts):
    parsed = front_matter(text)
    sections = markdown_sections(parsed["body"], parsed["body_line"])
    leading = []
    if parsed["raw"] is not None:
        inner = parsed["raw"].splitlines()[1:-1]
        leading.append(
            {
                "heading": FRONT_MATTER_HEADING,
                "text": "\n".join(inner).strip(),
                "locator": f"L1-L{parsed['body_line'] - 1}",
            }
        )
    first_h1 = next((s["heading"] for s in sections if s["level"] == 1), None)
    stated = parsed["fields"].get("title")
    title = (
        entry.get("title")
        or (stated if isinstance(stated, str) and stated else None)
        or first_h1
        or PurePosixPath(parts[-1]).stem
    )
    document = {"title": title, "sections": numbered(leading + sections)}
    if parsed["fields"]:
        document["front_matter"] = parsed["fields"]
    return document, parsed["missing"]


def _export_document(entry, text, parts):
    try:
        value = json.loads(text)
        if entry["kind"] == "slack_export":
            title, sections = PurePosixPath(parts[-1]).stem, slack_sections(value)
        else:
            title, sections = confluence_sections(value)
    except ValueError:
        raise Unreadable("unreadable_export") from None
    return {"title": entry.get("title") or title, "sections": numbered(sections)}, []


def read_file(base, entry, now):
    """One manifest file entry to a document without id or run. Raises Unreadable."""
    parts = relative_parts(entry["path"])
    data = read_bytes(base, parts)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise Unreadable("not_utf8") from None
    build = _text_document if entry["kind"] in TEXT_KINDS else _export_document
    document, missing = build(entry, text, parts)
    locator = "/".join(parts)
    return {
        "kind": entry["kind"],
        **document,
        "provenance": {
            "source_id": f"file:{locator}",
            "locator": locator,
            "content_sha256": records.sha256(data),
            "retrieved_at": now,
        },
        "missing": missing,
    }, len(data)
