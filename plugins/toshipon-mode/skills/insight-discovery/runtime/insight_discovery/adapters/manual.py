"""Hand-written exports: a JSON document or a small Markdown file.

Notes, constraints, things someone noticed, and hypotheses kept in other tools (exported by
hand) all enter here. Markdown shape:

    classification: synthetic
    ## task-key | domain-tag
    - [friction] what happened
    - [retry:rerun-after-late-check] what happened
"""

import re
from collections import Counter

from ..foundation import SCHEMAS, HarnessError, Refusal, agents, sha256, validate
from .base import Unrecognized, listed, load_object, read_text, safe_path

HEADING = re.compile(r"^##\s+([^|]+?)\s*(?:\|\s*(.+?)\s*)?$")
ITEM = re.compile(r"^[-*]\s+\[([a-z_]+)(?::([^\]]+))?\]\s+(.+)$")
CLASSIFICATION = re.compile(r"^classification:\s*([a-z]+)\s*$")


def _from_json(path, limits):
    document = load_object(path, limits)
    try:
        validate(f"{SCHEMAS}:stages#manual_export", document)
    except HarnessError:
        raise Unrecognized("not a manual export") from None
    classification = document.get("classification", "internal")
    return [{**item, "classification": classification} for item in document["items"]], 0


def _from_markdown(path, limits):
    raws, unparsed, task, declared = [], 0, None, []
    for line in read_text(path, limits).splitlines():
        line = line.rstrip()
        if not line or line.startswith("# "):
            continue
        if match := CLASSIFICATION.match(line):
            declared.append(match.group(1))
        elif match := HEADING.match(line):
            task = {"task_key": match.group(1), "domain_tag": match.group(2) or match.group(1)}
        elif (match := ITEM.match(line)) and task:
            raws.append({**task, "kind": match.group(1), "text": match.group(3)})
            if match.group(2):
                raws[-1]["label"] = match.group(2)
        else:
            unparsed += 1
    if not raws:
        raise Unrecognized("no items")
    # Several declarations in one file: the strictest wins, and an unknown one excludes the items.
    known = [name for name in declared if name in agents.CLASSIFICATIONS]
    if len(known) != len(declared):
        classification = next(name for name in declared if name not in known)
    else:
        classification = agents.strictest(*known) if known else "internal"
    return [{**raw, "classification": classification} for raw in raws], unparsed


def collect(path, context):
    if not path:
        raise HarnessError("the manual adapter needs --path (a .json or .md file, or a directory)")
    stats, limits = Counter(), context["limits"]
    target = safe_path(path)
    if target.is_dir():
        files = listed(target, ["*.json", "*.md"], limits, stats)
    elif target.is_file():
        files = [target]
    else:
        stats["source_missing"] += 1
        return [], stats
    raws = []
    for file in files:
        try:
            reader = _from_json if file.suffix == ".json" else _from_markdown
            items, unparsed = reader(file, limits)
        except Unrecognized:
            stats["format_unrecognized"] += 1
            continue
        except Refusal:
            if len(files) == 1:
                raise
            stats["oversize"] += 1
            continue
        stats["unparsed_lines"] += unparsed
        locator = f"manual/{sha256(file.name)[:12]}"
        raws += [{**item, "locator": locator} for item in items]
    return raws, stats
