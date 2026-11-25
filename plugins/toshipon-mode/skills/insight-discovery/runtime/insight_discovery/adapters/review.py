"""Review findings kept by the pr-feedback command (`*_review_findings.md`).

One evidence item per finding heading. The file name carries a ticket id and a PR number,
so it is stored hashed. The body kept is the line that states the problem.
"""

import re
from collections import Counter

from ..foundation import HarnessError, Refusal, sha256
from .base import Unrecognized, listed, read_text, safe_path

ROUND = re.compile(r"^#{1,4}\s*(\d+)\s*巡目")
FINDING = re.compile(r"^#{3,4}\s+(.+?)\s*$")
PROBLEM = re.compile(r"^[-*\s]*\**問題\**\s*[:：]\s*(.+?)\s*$")


def _findings(path, limits):
    findings, current = [], None
    for line in read_text(path, limits).splitlines():
        if ROUND.match(line):
            current = None
        elif (match := FINDING.match(line)) and "一覧" not in match.group(1):
            current = {"title": match.group(1), "problem": None}
            findings.append(current)
        elif current and (match := PROBLEM.match(line)) and not current["problem"]:
            current["problem"] = match.group(1)
    if not findings:
        raise Unrecognized("no finding headings")
    return findings


def collect(path, context):
    if not path:
        raise HarnessError("the review adapter needs --path (a findings file or a directory)")
    stats, limits = Counter(), context["limits"]
    target = safe_path(path)
    if target.is_dir():
        files = listed(target, ["*_review_findings.md"], limits, stats)
    elif target.is_file():
        files = [target]
    else:
        stats["source_missing"] += 1
        return [], stats
    raws = []
    for file in files:
        try:
            findings = _findings(file, limits)
        except Unrecognized:
            stats["format_unrecognized"] += 1
            continue
        except Refusal:
            if len(files) == 1:
                raise
            stats["oversize"] += 1
            continue
        source = sha256(file.stem)[:12]
        for index, finding in enumerate(findings):
            raws.append(
                {
                    "kind": "review_finding",
                    "label": "review:finding",
                    "task_key": f"review:{source}",
                    "domain_tag": "code-review",
                    "text": finding["problem"] or finding["title"],
                    "unit": sha256(f"{source}|{index}")[:16],
                    "locator": f"review/{source}/{index + 1}",
                    "classification": "internal",
                    "missing": [] if finding["problem"] else ["problem"],
                }
            )
    return raws, stats
