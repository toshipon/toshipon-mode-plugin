"""`spec report`: findings.json and trace.json to impact-report.json, impact-report.md and the
candidate fixes in `patches/` (`patch`).

No network and no model. git runs only against the bare repository `spec analyze` filled, to
read the files a fix changes and to check each diff in a temporary index; nothing is fetched,
so whether a head moved after the analysis is not known here (`spec check-patch` re-checks it
right before a patch is applied). The Markdown carries external text (PR titles, quotes from
documents, values from files): in a table cell it is escaped so that no link, image, HTML or
column break survives, and anything longer goes into a fenced code block whose fence is
longer than any backtick run in the text.

With a current `audit.json` (`spec audit`), `audit` carries its axes and the Markdown a table
per axis, with the backend and model that made it and its limitations in full right below the
table; without one, with one made from other input, or with one whose status does not follow
from its findings, every axis is `not_assessed`. A blocked, failed or not_assessed audit makes
the report exit 1, as a stale or partial analysis does.

`--format evaluator-output` prints, instead of the summary, the JSON an evaluator adapter of
the toshipon-mode Autonomous Improvement Harness v2 prints (`evaluation.schema.json`,
`$defs/output`): counts per finding kind, stale and unanalyzed PRs, and one evidence entry
per finding that points into impact-report.json, and with a current audit the number of audit
findings per axis.
"""

import re

from .. import paths, records
from ..errors import InputError
from . import audit as audit_stage
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
        "audit": audit_stage.load(run_dir)[0],
        "limitations": limitations,
    }
    records.validate("impact-report", report)
    return report, links


def audit_view(report):
    """The report's audit, or every axis not_assessed for a report made without one."""
    return report.get("audit") or audit_stage.view(None, "not_run")


def blocked(report):
    """Whether `spec report` exits 1: an incomplete analysis, or an audit that blocks, failed or
    assessed nothing."""
    return report["status"] != "complete" or audit_view(report)["status"] in (
        "blocked",
        "failed",
        "not_assessed",
    )


def _entities(item):
    return ", ".join(item["entities"]) or "-"


def _evidence_ids(item, basis):
    ids = [entry["id"] for entry in item["evidence"] if entry["basis"] == basis]
    return ", ".join(ids) or "-"


AUDIT_STATUS = {
    "passed": "合格。監査の critical / high も、監査が退けていない分析の critical / high もない",
    "blocked": "不合格。監査の critical か high、または監査が退けていない分析の critical か high がある",
    "failed": "不合格。上限の回数までに検査を通る答えが無かった",
    "not_assessed": "不合格。評価できた観点が 1 つも無い",
    "not_run": "未実施。`spec audit` を実行していない",
    "stale": "古い。audit.json が今の入力から作られていないか、記録が findings と食い違う",
}
AUDIT_BACKEND = {
    "stub": "規則だけの監査で、モデルは使っていません。文書は読まず、分析の重大な指摘を退けません",
    "claude": "Claude CLI による監査です",
}


def _audit_markdown(view, audited):
    text = "\n## 独立評価（audit）\n\n"
    text += f"- 状態: {view['status']}（{AUDIT_STATUS[view['status']]}）\n"
    if view["backend"] is not None:
        model = cell(view["model"]) if view["model"] else "CLI の既定（`--model` 未指定）"
        model = model if view["backend"] == "claude" else "なし"
        text += (
            f"- backend: {view['backend']}（{AUDIT_BACKEND[view['backend']]}）、model: {model}\n"
        )
    if view["analysis_blocking"]:
        text += (
            f"- 監査が退けていない分析の critical / high: {', '.join(view['analysis_blocking'])}\n"
        )
    text += "- 観点ごとの status は finding の severity から規則で決まります（LLM の判定ではありません）。"
    text += "critical か high があれば fail、medium と low だけなら concerns、finding が無く監査できた観点は pass、"
    text += "監査できなかった観点は not_assessed です。\n\n"
    text += "| 観点 | status | finding |\n|---|---|---|\n"
    for axis, entry in view["axes"].items():
        text += f"| {axis} | {entry['status']} | {', '.join(entry['finding_ids']) or '-'} |\n"
    if view["limitations"]:
        text += "\naudit の limitations（全文。重大な指摘が紛れていないかを人が読みます）:\n\n"
        text += "".join(block(line) for line in view["limitations"])
    if audited is None:
        return text
    if audited["findings"]:
        text += (
            "\n| ID | 観点 | 重大度 | 指摘 | 根拠 | 次に確かめること |\n|---|---|---|---|---|---|\n"
        )
        for item in audited["findings"]:
            text += (
                f"| {item['id']} | {item['category']} | {item['severity']} | "
                f"{cell(item['claim'])} | {cell('; '.join(item['evidence']))} | "
                f"{cell(item['next_experiment'])} |\n"
            )
    if audited["rejected_findings"]:
        text += "\n検査を通らず除いた指摘:\n\n| ID | 観点 | 重大度 | 理由 |\n|---|---|---|---|\n"
        for item in audited["rejected_findings"]:
            text += (
                f"| {item['id']} | {item['category']} | {item['severity']} | "
                f"{cell('; '.join(item['reasons']))} |\n"
            )
    if audited["reviewed"]:
        text += "\n監査が確かめた分析の指摘:\n\n| 分析の指摘 | 見解 | メモ |\n|---|---|---|\n"
        for entry in audited["reviewed"]:
            text += f"| {entry['ref']} | {entry['verdict']} | {cell(entry['note'])} |\n"
    notes = [f"{entry['axis']}: {entry['reason']}" for entry in audited["not_assessed"]]
    if notes:
        text += "\n監査できなかった観点:\n\n"
        text += "".join(f"- {cell(line)}\n" for line in notes)
    return text


def markdown(report, links, audited=None):
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
    text += _audit_markdown(audit_view(report), audited)
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
    audited = audit_view(report)
    if audited["status"] in ("passed", "blocked"):
        for axis, entry in audited["axes"].items():
            metrics[f"audit_findings_{axis}"] = float(len(entry["finding_ids"]))
        metrics["audit_findings_blocking"] = float(len(audited["blocking"]))
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
            "open PRs. Inferred trace links never raise a finding above medium. "
            f"Audit: {audited['status']}; its axis status follows the severities by rule."
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
    text = markdown(value, links, audit_stage.load(run_dir)[1])
    paths.write_private(paths.child(run_dir, "impact-report.md"), text)
    return value, text
