"""The local HTML explorer: one self-contained file. The index goes in as JSON that the page
reads with JSON.parse and places with textContent. The page loads nothing and connects to
nothing; fetching sources stays in the agent session, with the prompt the page lets you copy."""

import base64
import hashlib
import html
import json
from pathlib import Path

from .base import now
from .index import TYPES, edges
from .packet import build
from .redaction import redact
from .traverse import explore

ASSETS = Path(__file__).resolve().parent / "assets"
PROMPT_HOPS, PROMPT_ENTITIES, PROMPT_TOKENS = 1, 8, 4000


def _csp_hash(text):
    return (
        "'sha256-"
        + base64.b64encode(hashlib.sha256(text.encode("utf-8")).digest()).decode("ascii")
        + "'"
    )


def embed_json(value):
    """JSON that is inert inside a <script> element: no `<`, `>` or `&` survives as itself."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    for raw, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        text = text.replace(raw, escaped)
    return text.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")


def explorer_data(index, checked, title):
    findings = {}
    for finding in checked["findings"]:
        findings.setdefault(finding["entity"], []).append(
            {"kind": finding["kind"], "detail": finding["detail"]}
        )
    entities = []
    for name in sorted(
        index["entities"], key=lambda n: (TYPES.index(index["entities"][n]["type"]), n)
    ):
        entity = index["entities"][name]
        packet = build(
            explore(
                index,
                entity=name,
                hops=PROMPT_HOPS,
                max_entities=PROMPT_ENTITIES,
                token_budget=PROMPT_TOKENS,
            ),
            question="",
            mode="deep-dive",
            findings=checked["findings"],
        )
        source = entity.get("source") or {}
        entities.append(
            {
                "id": name,
                "type": entity["type"],
                "description": entity["description"],
                "config_path": entity.get("config_path"),
                "status": entity.get("status"),
                "source": {"locator": source.get("locator"), "revision": source.get("revision")},
                "findings": findings.get(name, []),
                "prompt": packet["prompt"],
            }
        )
    return redact(
        {
            "title": title,
            "generated_at": now(),
            "head": checked["head"],
            "types": list(TYPES),
            "entities": entities,
            "edges": [list(edge) for edge in edges(index)],
            "gaps": len(checked["findings"]),
        }
    )


def render(index, checked, title="Project Context Explorer"):
    style = (ASSETS / "explorer.css").read_text(encoding="utf-8")
    script = (ASSETS / "explorer.js").read_text(encoding="utf-8")
    policy = (
        f"default-src 'none'; script-src {_csp_hash(script)}; style-src {_csp_hash(style)};"
        " img-src 'none'; connect-src 'none'; base-uri 'none'; form-action 'none'"
    )
    data = embed_json(explorer_data(index, checked, title))
    return f"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="{html.escape(policy, quote=True)}">
<meta name="referrer" content="no-referrer">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<style>{style}</style>
</head>
<body>
<header>
<h1 id="title"></h1>
<p class="meta" id="summary"></p>
<p class="note">この HTML は静的で、資料を取りに行かない。資料の取得は、コピーした context prompt を貼った agent のセッションが行う。</p>
</header>
<main>
<section aria-label="検索">
<input type="search" id="search" placeholder="id・type・説明・path を検索" aria-label="metadata を検索">
<p class="note" id="count"></p>
<ul id="results"></ul>
</section>
<section aria-label="構成図"><div id="graph-box"><svg id="graph" role="group" aria-label="entity と関係"></svg></div></section>
<section aria-label="詳細" id="detail"><p class="note">ノードを選ぶと詳細を表示する。</p></section>
</main>
<script id="explorer-data" type="application/json">{data}</script>
<script>{script}</script>
</body>
</html>
"""
