# 言い換え語の記録

合成データの言い換えの case の query（`--write-expansion-input` の出力。case の ID と query だけ）を見て、agent が同じ意味の別の言い方と、文書に書かれそうな用語に広げた記録です。`knowledge_eval --paraphrase --expansions` の `caller-expansion` の再現に使います。

- 作った日: 2026-11-27
- 広げた agent: Haiku が 5 回（`haiku-1.json` から `haiku-5.json`）、Sonnet が 5 回（`sonnet-1.json` から `sonnet-5.json`）
- 渡した指示: 各 query について、同じ意味の別の言い方、その話題を文書に書くときに使われそうな用語を挙げる。日本語の語と英語の語の両方を含め、1 query あたり 8〜12 個。1 語は短い名詞句（64 文字以内）。検索対象の文書は見ない。
- 形: `{"model": "...", "run": N, "terms": {"PARA-0001": ["語", ...]}}`

合成データの query に対する記録で、実際の作業の query ではありません。
