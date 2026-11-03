"""Render Doc-workspace-compatible Markdown from structured candidates."""

from pathlib import Path


def text(value):
    return str(value).replace("\r", " ").replace("\n", " ").replace("|", "\\|")


def tags(requirement):
    return "".join(f"[{sid}]" for sid in requirement["source_refs"])


def render(input_data, design, report, out, context):
    out = Path(out)
    original = {r["id"]: r for r in design["requirements"]}
    heading = (
        f"# {text(input_data['project'])}\n\n"
        "## How to read this\n\n"
        "この文書は構造化 JSON の view です。文書品質 gate の結果は、業務承認や実測達成を意味しません。\n\n"
    )
    requirements = heading + "## 非機能要件\n\n"
    for requirement in design["requirements"]:
        requirements += (
            f"- {requirement['id']}. {text(requirement['description'])}。"
            f"目標 {requirement['target']} {text(requirement['unit'])}。"
            f"指標 {requirement['metric']}。{tags(requirement)}\n"
        )
    architecture = heading + "## AWS 構成\n\n"
    architecture += f"Region は {text(input_data['region'])} です。\n\n"
    architecture += "| 項目 | 設計値 |\n|---|---|\n"
    for key, value in design["aws"].items():
        architecture += f"| {key} | {text(value)} |\n"
    architecture += "\n## 決定と代替案\n\n| ID | 対象要件 | 設計判断 | 代替案 | 技術根拠 | 出典 |\n|---|---|---|---|---|---|\n"
    for decision in design["decisions"]:
        source_tags = "".join(
            sorted(
                {
                    f"[{sid}]"
                    for rid in decision["requirement_ids"]
                    for sid in original[rid]["source_refs"]
                }
            )
        )
        architecture += (
            f"| {decision['id']} | {', '.join(decision['requirement_ids'])} | "
            f"{text(decision['description'])} | {text('; '.join(decision['alternatives']))} | "
            f"{', '.join(decision['evidence_ids'])} | {source_tags} |\n"
        )
    architecture += "\n## 技術根拠\n\n"
    for evidence in design["evidence"]:
        evidence_tags = "".join(
            f"[{source['id']}]"
            for source in context["sources"]
            if source["locator"] == evidence["url"]
        )
        architecture += (
            f"- {evidence['id']}。{text(evidence['claim'])}。"
            f"{text(evidence['url'])}。確認者 {text(evidence['verified_by'])}、"
            f"確認日 {evidence['checked_at']}。{evidence_tags}\n"
        )
    verification = heading + "## 検証計画\n\n"
    for item in design["verifications"]:
        verification += (
            f"### {item['id']} と {item['requirement_id']}\n\n"
            f"{text(item['method'])}\n\n合否条件は {text(item['acceptance'])}。"
            f"目標 {item['target']} {text(item['unit'])}。"
            f"{tags(original[item['requirement_id']])}\n\n"
        )
    verification += "## 文書評価\n\n"
    verification += f"文書品質 gate は {'PASS' if report['passed'] else 'FAIL'} です。\n\n"
    for finding in report["findings"]:
        verification += (
            f"- {finding['id']} [{finding['severity']}]。{text(finding['description'])}\n"
        )
    for limitation in report["limitations"]:
        verification += f"- {text(limitation)}\n"
    shared = "\n## 前提\n\n"
    for assumption in design["assumptions"]:
        shared += f"- {text(assumption)}\n"
    shared += "\n## 未決事項\n\n"
    unresolved = False
    for question in context["questions"]:
        if question["status"] in {"open", "asked", "assumed"}:
            unresolved = True
            marker = " 仮" if question["status"] == "assumed" else ""
            shared += (
                f"- [{question['id']}{marker}] {text(question['question'])}。"
                f"担当 {text(question['owner'])}。状態 {question['status']}。\n"
            )
    for issue in design["open_issues"]:
        if issue["status"] == "open":
            unresolved = True
            shared += f"- {issue['id']} [{issue['severity']}]。{text(issue['description'])}\n"
    if not unresolved:
        shared += "この候補に記録された未決事項はありません。\n"
    shared += "\n## Appendix A. 出典一覧\n\n| ID | 種別 | 出典 | 取得日 |\n|---|---|---|---|\n"
    for source in context["sources"]:
        shared += (
            f"| {source['id']} | {text(source['kind'])} | "
            f"{text(source['locator'])} | {text(source['retrieved'])} |\n"
        )
    for name, content in [
        ("requirements.md", requirements),
        ("design.md", architecture),
        ("verification.md", verification),
    ]:
        (out / name).write_text(content + shared, encoding="utf-8")
