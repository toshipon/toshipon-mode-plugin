"""What a session is handed: an overview of the index, or a context packet for one entity and
one question. A packet carries metadata, references and how to fetch them. It carries no
document body, so a new or forked session starts small and fetches what it needs."""

import shlex

from .base import Refusal, estimate_tokens
from .claims import parse_locator
from .redaction import redact
from .untrusted import PREAMBLE, envelope, one_line

MODES = ("overview", "deep-dive", "learning")
SECTIONS = (
    ("objectives", ("objective",)),
    ("architecture", ("component", "resource", "property")),
    ("requirements", ("requirement",)),
    ("decisions", ("decision",)),
    ("changes", ("change",)),
    ("risks", ("risk",)),
    ("unresolved_questions", ("question",)),
)
RULES = (
    "読み取りだけを行う。書き込み、コメントの resolve、デプロイ、外部への送信はしない。",
    "主張ごとに status（confirmed / inferred / conflict / unknown）、locator、revision を付ける。"
    "confirmed には引用を付ける。",
    "設定値だけから過去の理由を推測しない。理由を書いた資料が無ければ unknown と書く。",
    "ここに無い資料が要るときだけ範囲を広げ、広げた理由を書く。",
)


def overview(index, findings, per_section=5):
    """Each section's count and its first few entities. Short by default; `more` says how much is left."""
    sections = {}
    for name, types in SECTIONS:
        members = sorted(
            (entity for entity in index["entities"].values() if entity["type"] in types),
            key=lambda entity: entity["id"],
        )
        sections[name] = {
            "count": len(members),
            "entities": [
                {"id": e["id"], "type": e["type"], "description": e["description"]}
                for e in members[:per_section]
            ],
            "more": max(0, len(members) - per_section),
        }
    by_kind = {}
    for finding in findings:
        by_kind.setdefault(finding["kind"], []).append(finding)
    return redact(
        {
            "sections": sections,
            "gaps": {
                kind: {"count": len(found), "first": found[:per_section]}
                for kind, found in sorted(by_kind.items())
            },
            "sources": index["sources"],
            "entities": len(index["entities"]),
        }
    )


def retrieval_instruction(step):
    """How to fetch one source, read-only. Tool names for documents are not assumed."""
    where = parse_locator(step["locator"])
    revision = step["revision"] or "HEAD"
    if where["kind"] == "git":
        span = f"L{where['start']}" + (
            f"-L{where['end']}" if where["end"] != where["start"] else ""
        )
        lines = f"（{span}）" if where["start"] else ""
        return f"git show {shlex.quote(revision + ':' + where['path'])}{lines}"
    if where["kind"] == "pr":
        what = f"レビューコメント {where['comment']}" if where["comment"] else "本文"
        return f"PR #{where['number']} の{what}を読み取り専用で取得する（head {revision}）"
    return (
        f"接続済みの文書ツールを探し、{where['source']} の {where['id']} を版 {revision} で取得する。"
        "接続が無ければ取得せず、unknown と記録する"
    )


def build(exploration, *, question, mode, findings, claims=None):
    if mode not in MODES:
        raise Refusal(f"mode must be one of {', '.join(MODES)}")
    if not exploration["seeds"]:
        raise Refusal("no entity matches the question; add entities or name one with --entity")
    chosen = {entity["id"] for entity in exploration["entities"]}
    packet = {
        "selected_entity": exploration["seeds"][0],
        "question": one_line(question, 400),
        "mode": mode,
        "references": exploration["entities"],
        "edges": exploration["edges"],
        "retrieval_instructions": [
            {**step, "instruction": retrieval_instruction(step)}
            for step in exploration["retrieval"]
        ],
        "deferred": exploration["deferred"],
        "frontier": exploration["frontier"],
        "token_budget": exploration["bounds"],
        "claims": claims or [],
        "gaps": [f for f in findings if f["entity"] in chosen or f["entity"] is None],
    }
    packet = redact(packet)
    packet["prompt"] = render_prompt(packet)
    packet["token_budget"] = {
        **packet["token_budget"],
        "prompt_tokens": estimate_tokens(len(packet["prompt"].encode("utf-8"))),
    }
    return packet


def _source(entity):
    source = entity.get("source")
    if not source:
        return "出典なし"
    return f"{source['locator']}@{source.get('revision', '?')}"


def render_prompt(packet):
    """The packet as text to paste into a new session. Everything that came from a source
    sits inside one envelope; the instructions around it are fixed text."""
    data = ["# metadata: id | type | 距離 | 出典 | 説明"]
    data += [
        f"{e['id']} | {e['type']} | {e['distance']} | {_source(e)} | {e['description']}"
        for e in packet["references"]
    ]
    if packet["claims"]:
        data.append("# claims: status | id | 主張 | 出典")
        for claim in packet["claims"]:
            sources = ", ".join(
                f"{r['locator']}@{r['revision']}" for r in claim.get("references", [])
            )
            data.append(
                f"{claim['status']} | {claim['id']} | {one_line(claim['text'])} | {sources or '-'}"
            )
    budget = packet["token_budget"]
    lines = [
        f"# Context packet: {packet['selected_entity']}（{packet['mode']}）",
        "",
        PREAMBLE,
        "",
        "## 問い",
        envelope("question", packet["question"] or "（問いは未指定）"),
        "",
        "## 参照（metadata だけで、本文は含まない）",
        envelope("index", "\n".join(data)),
        "",
        "## 関係",
        *(
            [f"- {start} --{relation}--> {end}" for start, relation, end in packet["edges"]]
            or ["- なし"]
        ),
        "",
        f"## 取得手順（予算 {budget['token_budget']} tokens、見積もり {budget['estimated_tokens']} tokens）",
        *(
            [
                f"{n}. [{s['entity']}] {s['instruction']}"
                for n, s in enumerate(packet["retrieval_instructions"], 1)
            ]
            or ["- 取得する出典は無い"]
        ),
    ]
    if packet["deferred"]:
        lines.append(
            "- 予算に入らず後回し: " + ", ".join(step["entity"] for step in packet["deferred"])
        )
    if packet["frontier"]:
        lines.append("- 範囲の外（hop を増やすと届く）: " + ", ".join(packet["frontier"]))
    lines += ["", "## 欠落と古い参照"]
    lines += [
        f"- {gap['kind']}: {gap['entity'] or '-'}: {gap['detail']}" for gap in packet["gaps"]
    ] or ["- なし"]
    lines += ["", "## 守ること", *[f"- {rule}" for rule in RULES]]
    return "\n".join(lines) + "\n"
