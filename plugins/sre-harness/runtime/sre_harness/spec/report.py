"""`spec report`: findings.json and trace.json to impact-report.json, impact-report.md and the
candidate fixes in `patches/` (`patch`).

No network and no model. git runs only against the bare repository `spec analyze` filled, to
read the files a fix changes and to check each diff in a temporary index; nothing is fetched,
so whether a head moved after the analysis is not known here (`spec check-patch` re-checks it
right before a patch is applied). The Markdown carries external text (PR titles, quotes from
documents, values from files): in a table cell it is escaped so that no link, image, HTML or
column break survives, and anything longer goes into a fenced code block whose fence is
longer than any backtick run in the text.

`--format evaluator-output` prints, instead of the summary, the JSON an evaluator adapter of
the toshipon-mode Autonomous Improvement Harness v2 prints (`evaluation.schema.json`,
`$defs/output`): counts per finding kind, stale and unanalyzed PRs, and one evidence entry
per finding that points into impact-report.json.
"""

import re

from .. import paths, records
from ..errors import InputError
from . import findings as fd
from . import patch as patch_stage
from . import prs as git_prs

_MARKUP = re.compile(r"([\\`*_\[\]()!|~])")


def cell(value):
    """Plain text that a Markdown table shows as written: no link, image, HTML or new cell."""
    text = " ".join(str(value).split())
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = _MARKUP.sub(r"\\\1", text)
    return text.replace("://", ":&#47;&#47;")


def block(value):
    """A fenced code block that the text cannot close."""
    text = str(value)
    runs = [len(run) for run in re.findall(r"`+", text)]
    fence = "`" * max(3, max(runs, default=0) + 1)
    return f"{fence}text\n{text}\n{fence}\n"


def _read(run_dir):
    target = paths.child(run_dir, "findings.json")
    if not target.is_file():
        raise InputError("the run has no findings yet: run `spec analyze` first.")
    found = records.read_json(target)
    records.validate("impact-report", found, "findings_file")
    links = records.read_json(paths.child(run_dir, "trace.json"))
    records.validate("trace-link", links, "file")
    return found, links


def build(run_dir):
    found, links = _read(run_dir)
    explicit = sum(1 for item in links["links"] if item["basis"] == "explicit")
    limitations = list(found["limitations"])
    limitations.append(
        "`spec report` does not fetch: whether the default branch or a PR head moved after "
        "`spec analyze` is not re-checked here (default_branch.moved_since_analysis stays "
        "false). Each patch names the SHA it was made for; run `spec check-patch` right before "
        "applying one, which fetches again and refuses a moved head with exit 3."
    )
    limitations.append(
        "inferred trace links come from the backend or its stub; they are listed apart and "
        "never the only ground of a critical or high finding."
    )
    report = {
        "schema_version": records.SCHEMA_VERSION,
        "run_id": found["run_id"],
        "generated_at": records.iso(),
        "criteria_hash": found["criteria_hash"],
        "repo": found["repo"],
        "status": found["status"],
        "default_branch": {**found["default_branch"], "moved_since_analysis": False},
        "prs": found["prs"],
        "findings": found["findings"],
        "trace": {
            "ref": "trace.json",
            "explicit": explicit,
            "inferred": len(links["links"]) - explicit,
        },
        "limitations": limitations,
    }
    records.validate("impact-report", report)
    return report, links


def _entities(item):
    return ", ".join(item["entities"]) or "-"


def _evidence_ids(item, basis):
    ids = [entry["id"] for entry in item["evidence"] if entry["basis"] == basis]
    return ", ".join(ids) or "-"


def markdown(report, links):
    counts = {kind: 0 for kind in fd.KINDS}
    for item in report["findings"]:
        counts[item["kind"]] += 1
    branch = report["default_branch"]
    text = f"# 影響範囲の報告 {cell(report['run_id'])}\n\n"
    text += f"- リポジトリ: {cell(report['repo'])}\n"
    text += f"- default branch: {cell(branch['name'])} {branch['sha']}\n"
    text += f"- 状態: {report['status']}\n- criteria_hash: {report['criteria_hash']}\n\n"
    text += "外部入力（PR タイトル、文書の引用、ファイルの値）はエスケープするか、コードブロックに入れています。\n\n"
    text += "## 件数\n\n| 種類 | 件数 |\n|---|---|\n"
    text += "".join(f"| {kind} | {count} |\n" for kind, count in counts.items())
    text += "\n## PR\n\n| PR | タイトル | head | merge base | stale | 欠落 |\n|---|---|---|---|---|---|\n"
    for pr in report["prs"]:
        text += (
            f"| #{pr['number']} | {cell(pr.get('title', ''))} | {pr['head_sha'] or '-'} | "
            f"{pr['merge_base'] or '-'} | {'yes' if pr['stale'] else 'no'} | "
            f"{cell(pr.get('missing', '')) or '-'} |\n"
        )
    text += "\n## 指摘\n\n"
    text += "| ID | 種類 | 重大度 | 根拠 | entity | PR | 根拠（explicit） | 根拠（inferred） |\n"
    text += "|---|---|---|---|---|---|---|---|\n"
    for item in report["findings"]:
        prs = ", ".join(f"#{n}" for n in item["prs"]) or "-"
        text += (
            f"| {item['id']} | {item['kind']} | {item['severity']} | {item['basis']} | "
            f"{cell(_entities(item))} | {prs} | {_evidence_ids(item, 'explicit')} | "
            f"{_evidence_ids(item, 'inferred')} |\n"
        )
    text += "\n## 指摘の詳細\n\n"
    for item in report["findings"]:
        text += f"### {item['id']} {item['kind']}\n\n説明:\n\n{block(item['explanation'])}\n"
        text += "| 証跡 | 根拠 | 参照 |\n|---|---|---|\n"
        for entry in item["evidence"]:
            text += f"| {entry['id']} | {entry['basis']} | {cell(entry['ref'])} |\n"
        notes = "\n".join(f"{e['id']}: {e['note']}" for e in item["evidence"] if e.get("note"))
        if notes:
            text += f"\n引用と値:\n\n{block(notes)}"
        text += "\n"
    text += "## トレース\n\n| from | to | explicit | inferred |\n|---|---|---|---|\n"
    pairs = {}
    for item in links["links"]:
        key = (
            f"{item['from']['type']} {item['from']['id']}",
            f"{item['to']['type']} {item['to']['id']}",
        )
        pairs.setdefault(key, {"explicit": [], "inferred": []})[item["basis"]].append(
            f"{item['id']} {item['relation']}"
        )
    for (source, target), found in sorted(pairs.items()):
        text += (
            f"| {cell(source)} | {cell(target)} | {cell(', '.join(found['explicit'])) or '-'} | "
            f"{cell(', '.join(found['inferred'])) or '-'} |\n"
        )
    text += _patches_markdown(report)
    text += "\n## 制約\n\n" + "".join(f"- {cell(line)}\n" for line in report["limitations"])
    return text


def evaluator_output(report):
    counts = {kind: 0 for kind in fd.KINDS}
    for item in report["findings"]:
        counts[item["kind"]] += 1
    metrics = {f"findings_{kind}": float(count) for kind, count in counts.items()}
    metrics["findings_total"] = float(len(report["findings"]))
    metrics["findings_inferred"] = float(
        sum(1 for item in report["findings"] if item["basis"] == "inferred")
    )
    metrics["prs_total"] = float(len(report["prs"]))
    metrics["prs_stale"] = float(sum(1 for pr in report["prs"] if pr["stale"]))
    metrics["prs_unanalyzed"] = float(sum(1 for pr in report["prs"] if pr.get("missing")))
    output = {
        "metrics": metrics,
        "evidence": [
            {
                "id": item["id"],
                "ref": f"impact-report.json#{item['id']}",
                "note": f"{item['kind']} {item['severity']} {item['basis']}",
            }
            for item in report["findings"]
        ],
        "notes": (
            "Counts are computed by fixed rules from the registry, the default branch and the "
            "open PRs. Inferred trace links never raise a finding above medium."
        ),
    }
    analyzed = len(report["prs"]) - metrics["prs_stale"] - metrics["prs_unanalyzed"]
    if report["prs"] and analyzed == 0:
        output["unevaluable"] = "no open PR could be analyzed (all stale or missing)."
    return output


def _target(item):
    return f"PR #{item['target_pr']}" if item.get("target_pr") else "default branch"


def _patches_markdown(report):
    patches, rejected = report.get("patches", []), report.get("patch_rejections", [])
    text = "\n## 修正候補（patch）\n\n"
    text += (
        "harness は patch を適用しません。適用する前に `spec check-patch --run <run_id> "
        "--patch <PT-id>` を実行し、対象の head が動いていないこと（動いていれば exit 3）と、"
        "一時 index で `git apply --cached --check` が通ることを確かめてください。\n\n"
    )
    if patches:
        text += "| patch | 指摘 | 対象 | head | path | apply check |\n|---|---|---|---|---|---|\n"
        for item in patches:
            text += (
                f"| {item['id']} | {item['finding_id']} | {_target(item)} | "
                f"{item['target_head_sha']} | {cell(', '.join(item['paths']))} | "
                f"{item['apply_check']} |\n"
            )
    else:
        text += "patch はありません。\n"
    if rejected:
        text += "\n出さなかった修正候補:\n\n| 指摘 | 対象 | path | 理由 | 詳細 |\n"
        text += "|---|---|---|---|---|\n"
        for item in rejected:
            text += (
                f"| {item['finding_id']} | {_target(item)} | {cell(item.get('path', '-'))} | "
                f"{item['reason']} | {cell(item['detail'])} |\n"
            )
    return text


def _repository(run_dir, root, report):
    def open_repository():
        analysis = records.read_json(paths.child(run_dir, "analyze.json"))
        if not isinstance(analysis.get("source"), str):
            raise InputError("analyze.json names no git source: run `spec analyze` again.")
        owner, _, name = report["repo"].partition("/")
        return git_prs.Repository.existing(root, owner, name, analysis["source"])

    return open_repository


def report(run_dir, root):
    """(impact-report, markdown text). Both are written to the run, with the patches."""
    value, links = build(run_dir)
    patches, rejected = patch_stage.generate(run_dir, value, _repository(run_dir, root, value))
    refs = {item["finding_id"]: item["id"] for item in patches}
    for item in value["findings"]:
        if item["id"] in refs:
            item["patch_ref"] = refs[item["id"]]
    value["patches"], value["patch_rejections"] = patches, rejected
    records.validate("impact-report", value)
    records.write_json(paths.child(run_dir, "impact-report.json"), value)
    text = markdown(value, links)
    paths.write_private(paths.child(run_dir, "impact-report.md"), text)
    return value, text
