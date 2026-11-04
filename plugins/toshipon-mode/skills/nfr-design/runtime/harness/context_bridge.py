"""Bind generated designs to reviewed, versioned source context."""

import hashlib
import json
from pathlib import Path

from .claims import MAX_EXTRACTION_BYTES, assess_context, requirements_from_claims
from .intake import load_context, snapshot_hashes


def attach_context(workspace, input_data, ledger_sources):
    root = Path(workspace)
    if not any(
        (root / name).exists() or (root / name).is_symlink()
        for name in ["context.json", "claims.json"]
    ):
        if input_data.get("context_binding") or any(
            r.get("context_refs") for r in input_data["requirements"]
        ):
            return _failure("Context-bound input requires context.json and claims.json.")
        return {}
    try:
        context = load_context(root)
        claims_path = root / "claims.json"
        if claims_path.is_symlink() or not claims_path.is_file():
            raise ValueError("Extract and review claims before generating a design.")
        claims_bytes = claims_path.read_bytes()
        claims = json.loads(claims_bytes)
        documents = []
        size = 0
        for source in context["sources"]:
            text = (root / source["snapshot_path"]).read_text(encoding="utf-8")
            size += len(text.encode("utf-8"))
            if size > MAX_EXTRACTION_BYTES:
                raise ValueError("Source context exceeds the extraction and review size limit.")
            documents.append({**source, "text": text})
        report = assess_context(root, claims)
        hashes = snapshot_hashes(root, context)
        hashes["claims.json"] = hashlib.sha256(claims_bytes).hexdigest()
        binding = {"context_sha256": hashes["context.json"], "claims_sha256": hashes["claims.json"]}
        if input_data.get("context_binding") != binding:
            report["findings"].append(
                _finding(
                    "CONTEXT_BINDING",
                    "Source context or reviewed claims changed; prepare a fresh input snapshot.",
                )
            )
        if any(input_data[k] != context[k] for k in ["project", "region", "budget_monthly_usd"]):
            report["findings"].append(
                _finding(
                    "CONTEXT_PROJECT",
                    "Project, region or budget differs from the collected context.",
                )
            )
        expected = requirements_from_claims(claims)
        proposed = input_data["requirements"]
        if {r["id"]: r for r in proposed} != {r["id"]: r for r in expected}:
            report["findings"].append(
                _finding(
                    "CONTEXT_REQUIREMENTS",
                    "Requirements must exactly preserve all active approved context requirements.",
                )
            )
        ledger = {s["id"]: s for s in ledger_sources}
        for source in context["sources"]:
            row = ledger.get(source["id"])
            if (
                not row
                or any(row[k] != source[k] for k in ["locator", "title"])
                or row["retrieved"] != source["retrieved_at"][:10]
            ):
                report["findings"].append(
                    _finding(
                        "CONTEXT_LEDGER",
                        f"{source['id']} ledger no longer matches its collected provenance.",
                    )
                )
        report["passed"] = not any(
            f["severity"] in {"high", "critical"} for f in report["findings"]
        )
        return {
            "context_report": report,
            "context_sources": context,
            "context_documents": documents,
            "context_claims": claims,
            "context_hashes": hashes,
        }
    except (OSError, ValueError, KeyError, TypeError) as error:
        return _failure(f"Context cannot be trusted: {type(error).__name__}: {error}")


def _finding(code, description):
    return {"id": code, "severity": "high", "description": description}


def _failure(description):
    return {
        "context_report": {
            "passed": False,
            "findings": [_finding("CONTEXT_INVALID", description)],
            "conflicts": [],
            "gaps": [],
        },
        "context_hashes": {},
    }


def render_assessment(report, context=None, claims=None):
    def safe(value):
        return str(value).replace("\n", " ").replace("\r", " ").replace("|", "\\|")

    text = "# 文脈の整合性と承認\n\n最新の資料を自動で正解とせず、出典・現状・要件・承認を分けて記録します。\n\n"
    text += "## 出典と版\n\n| ID | 出典 | 版 | 取得時点 | 担当 |\n|---|---|---|---|---|\n"
    for source in (context or {}).get("sources", []):
        text += f"| {source['id']} | {safe(source['locator'])} | {safe(source['version'])} | {safe(source['retrieved_at'])} | {safe(source['owner'])} |\n"
    text += "\n## 抽出内容と承認\n\n| ID | 種類 | 対象・範囲・期間 | 内容 | 状態 | 出典 |\n|---|---|---|---|---|---|\n"
    for claim in (claims or {}).get("claims", []):
        refs = " ".join(f"[{r['source_id']}]" for r in claim["source_refs"])
        text += f"| {claim['id']} | {claim['kind']} | {safe(claim['subject'])} / {safe(claim['scope'])} / {safe(claim['period'])} | {safe(claim['summary'])} | {claim['approval']['status']} | {refs} |\n"
    text += "\n## 承認と置換\n\n| 内容 | 要件 ID | 確認者 | 確認時点 | 理由 | 置換した内容 |\n|---|---|---|---|---|---|\n"
    for claim in (claims or {}).get("claims", []):
        approval = claim["approval"]
        text += f"| {claim['id']} | {safe(claim['requirement_id'] or '')} | {safe(approval['reviewer'])} | {safe(approval['reviewed_at'])} | {safe(approval['reason'])} | {safe(', '.join(claim['supersedes']))} |\n"
    text += "\n## 引用と抽出した値\n\n"
    for claim in (claims or {}).get("claims", []):
        text += f"### {claim['id']}\n\n指標 {safe(claim['metric'])}。抽出値 {safe(claim['value'])} {safe(claim['unit'])}。\n\n"
        for ref in claim["source_refs"]:
            text += f"> {safe(ref['quote'])} [{ref['source_id']}]\n\n"
    text += "\n## 指摘と未決事項\n\n"
    for finding in report["findings"]:
        text += f"- [{finding['severity']}] {finding['id']}。{safe(finding['description'])}\n"
    if not report["findings"]:
        text += "この検査で未解決の指摘は検出されませんでした。\n"
    text += "\n承認者の本人確認や根拠本文の真偽判定は行いません。採用前に担当者が引用の意味と適用範囲を確認してください。\n"
    return text
