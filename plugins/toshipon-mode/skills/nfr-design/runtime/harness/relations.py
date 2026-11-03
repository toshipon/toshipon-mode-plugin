"""Model-suggested reading pairs across differently named claims.

assess compares only claims with an identical subject/metric/scope/period. Claims about
the same constraint often carry different names, so a model proposes candidates here.
Candidates never approve, reject or resolve a claim and never change the assess gate.
"""

import copy
import json
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from .claims import MAX_EXTRACTION_BYTES, ClaimsError, _claims_path, _digest, _group, _load, _schema

PAYLOAD_FIELDS = ("id", "kind", "subject", "metric", "value", "unit", "scope", "period", "summary")
LABELS = {
    "possible_conflict": "食い違いの候補",
    "depends_on_unresolved": "未決事項への依存",
    "overlap": "同じ対象の別表現",
}


def _payload(claims):
    items = []
    for claim in claims:
        item = {key: claim[key] for key in PAYLOAD_FIELDS}
        item["sources"] = [
            {"source_id": ref["source_id"], "quote": ref["quote"]} for ref in claim["source_refs"]
        ]
        items.append(item)
    return {"claims": items}


def _cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", " ")


def relate_claims(workspace, backend, out):
    path = _claims_path(workspace)
    if not path.is_file():
        raise ClaimsError("Extract claims before relating them.")
    target = Path(out).absolute()
    if target.is_symlink() or target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ClaimsError("Relation output must be a new or empty directory.")
    before = _digest(path)
    claims = _load(path)["claims"]
    payload = _payload(claims)
    if len(json.dumps(payload, ensure_ascii=False).encode("utf-8")) > MAX_EXTRACTION_BYTES:
        raise ClaimsError("Claims exceed the 1 MiB relation limit; split the workspace.")
    output = backend("relator", copy.deepcopy(payload), _schema("relations"))
    try:
        Draft202012Validator(_schema("relations")).validate(output)
    except ValidationError as error:
        raise ClaimsError("Relator output does not match the relations schema.") from error
    if _digest(path) != before:
        raise ClaimsError("claims.json changed while relating claims.")
    by_id = {claim["id"]: claim for claim in claims}
    relations, seen = [], set()
    for item in output["relations"]:
        ids = sorted(item["claim_ids"], key=lambda cid: int(cid[1:]))
        unknown = [cid for cid in ids if cid not in by_id]
        if unknown:
            raise ClaimsError(f"Relator referenced unknown claims: {', '.join(unknown)}.")
        if len({_group(by_id[cid]) for cid in ids}) == 1 or (tuple(ids), item["relation"]) in seen:
            continue
        seen.add((tuple(ids), item["relation"]))
        relations.append(
            {
                "id": f"R{len(relations) + 1}",
                "claim_ids": ids,
                "relation": item["relation"],
                "reason": item["reason"],
                "source_ids": sorted(
                    {ref["source_id"] for cid in ids for ref in by_id[cid]["source_refs"]}
                ),
                "status": "candidate",
            }
        )
    result = {"schema_version": 1, "claims_sha256": before, "relations": relations}
    target.mkdir(parents=True, exist_ok=True)
    with (target / "relations.json").open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    text = "# 名前の異なる主張の関連候補\n\n"
    text += "モデルが挙げた候補です。食い違いや依存の確定ではありません。assess の判定は変えません。\n\n"
    text += "| ID | 種類 | 主張 | 理由 |\n|---|---|---|---|\n"
    for rel in relations:
        claims_text = "<br>".join(
            _cell(
                f"{cid} [{by_id[cid]['kind']}] {by_id[cid]['subject']} / {by_id[cid]['metric']} = {by_id[cid]['value']}"
            )
            for cid in rel["claim_ids"]
        )
        text += f"| {rel['id']} | {LABELS[rel['relation']]} | {claims_text} | {_cell(rel['reason'])} |\n"
    if not relations:
        text += "\n候補はありません。\n"
    with (target / "relations.md").open("x", encoding="utf-8") as stream:
        stream.write(text)
    return result
