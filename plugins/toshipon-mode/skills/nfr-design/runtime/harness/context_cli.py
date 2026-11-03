"""CLI-facing context reports; collection/review never implies design approval."""

import hashlib
import json
from pathlib import Path

from .claims import assess_context, diff_context
from .intake import load_context, snapshot_hashes
from .workspace import LEDGERS, assert_unchanged, digest, prepare_output


def assess(workspace, out):
    root = Path(workspace)
    context = load_context(root)
    claims_path = root / "claims.json"
    if claims_path.is_symlink() or not claims_path.is_file():
        raise ValueError("Extract claims before assessing context.")
    claims_bytes = claims_path.read_bytes()
    claims = json.loads(claims_bytes)
    report = assess_context(root, claims)
    hashes = {
        **snapshot_hashes(root, context),
        "claims.json": hashlib.sha256(claims_bytes).hexdigest(),
    }
    ledger_hashes = {name: digest(root / name) for name in LEDGERS}
    assert_unchanged(root, {**hashes, **ledger_hashes})
    extras = {
        "context_hashes": hashes,
        "context_report": report,
        "context_sources": context,
        "context_claims": claims,
    }
    prepare_output(root, out, extras)
    assert_unchanged(root, {**hashes, **ledger_hashes})
    return report


def write_impact(before, after, input_data, design, out):
    report = diff_context(before, after, input_data, design)
    path = Path(out).absolute()
    if path.is_symlink() or path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError("Impact output must be a new or empty directory.")
    path.mkdir(parents=True, exist_ok=True)
    with (path / "impact.json").open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    text = "# 出典変更の影響\n\n"
    text += "| 出典 | 変更 | 変更前の版 | 変更後の版 |\n|---|---|---|---|\n"
    for item in report["changed_sources"]:

        def safe(v):
            return str(v).replace("|", "\\|").replace("\n", " ").replace("\r", " ")

        text += f"| {item['id']} | {item['change']} | {safe(item['before_version'])} | {safe(item['after_version'])} |\n"
    text += "\nこの一覧は見直し候補です。直接参照の影響と、共有文脈の変更から保守的に選んだ候補を分けています。\n"
    for item in report.get("context_metadata_changes", []):
        text += f"\n- 案件情報 {item['field']} が変更されています。\n"
    for key, label in [
        ("affected_requirements", "見直す要件"),
        ("directly_affected_requirements", "変更した出典を直接参照する要件"),
        ("potentially_affected_requirements", "共有文脈から見直す要件"),
        ("affected_decisions", "見直す設計判断"),
        ("affected_verifications", "見直す検証計画"),
        ("invalidated_claims", "承認を再確認する抽出内容"),
    ]:
        text += (
            f"\n## {label}\n\n" + (", ".join(report[key]) or "対応する項目はありません。") + "\n"
        )
    text += "\n元の要件や承認は自動変更しません。新しい出典で抽出・承認・設計評価をやり直してください。\n"
    with (path / "impact.md").open("x", encoding="utf-8") as stream:
        stream.write(text)
    return report
