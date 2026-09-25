---
name: japanese-writing
description: 日本語のドキュメントやブログ記事を書くとき、Gemini に推敲・レビューさせて 2 つのモデルの視点を通す。Use when writing or editing Japanese documents, blog posts, articles, README, release notes, or announcements.
---

# 日本語ドキュメントの執筆

日本語の文章は、1 つのモデルだけで書くと癖が残る。Claude が下書きし、Gemini にレビューさせ、Claude が統合する。**ブログ記事では必ずこの流れを通す。**

## 使うもの

この skill に同梱した `scripts/gemini-ja.sh` を使う。公式の Antigravity CLI (`agy`) のラッパーで、サインイン済みの枠を使うので API キーは要らない。`agy` は `PATH` 上のものを使い、見つからなければ `~/.local/bin/agy` を見る。別の場所に置いている場合は `AGY_BIN` で指定する。

```bash
"${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" "<プロンプト>"                      # 既定: gemini-3.8-flash-high
"${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high "<プロンプト>"  # 深い推論が要るとき
```

| 用途 | モデル |
|------|--------|
| 推敲・言い換え・短い判断 | `gemini-3.8-flash-high`（既定） |
| 構成レビュー・調査・長文の読解 | `gemini-3.1-pro-high` |
| 速度優先 | `gemini-3.6-flash-low` など |

**モデル一覧は `agy models` を信用しない。** 一部しか返らない。正確な一覧は不正なモデル名を渡したときのエラーに出る。

```bash
agy --model invalid -p=x 2>&1 | sed -n '/Available models/,$p'
```

長い本文はシェル引数に入れず stdin で渡す。

```bash
{
  echo "以下の記事を推敲してください。指摘は箇条書きで、修正案は原文と対にして示してください。"
  cat draft.md
} | "${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high
```

## 手順

| 段階 | 担当 | やること |
|------|------|----------|
| 1. 下書き | Claude | 構成と本文を書く |
| 2. レビュー | Gemini | 推敲・事実確認・構成の指摘 |
| 3. 統合 | Claude | 指摘を取捨選択して反映 |
| 4. 出典検証 | Claude | 数値・事実を書いたら `hdd:source-traceability`（別 plugin。未導入なら一次ソースを手で開いて照合する）で一次ソースと照合 |
| 5. 仕上げ | Claude | `toshipon-mode:japanese-tech-writing` で日本語の規範を点検し、`toshipon-mode:unslop` で表記の癖を除去 |

Gemini の指摘は**採用候補であって正解ではない**。矛盾する指摘は採らず、採らなかった理由を 1 行で述べる。

### 段階 5 の分担

`toshipon-mode:japanese-tech-writing` が日本語の論理・語り・翻訳調を、`toshipon-mode:unslop` が em dash や太字過多などの表記を扱う。守備範囲が違うので両方通す。

特に効くのは「推量として書いた文を機械的に断定へ変えない」という規範である。Gemini のレビューでも断定しすぎは繰り返し指摘される箇所で、先に自分で潰せる。

### 段階 4 を飛ばさない

この工程は実際の事故を受けて追加した。ある記事で、Gemini に Web 検索させて得たベンチマーク数値 3 件をそのまま本文に書いたところ、公開前に一次ソース（公式発表ページ）を開いて照合したら **3 件とも記載がなかった**。1 件は数値自体が存在せず、2 件はそのベンチマークに言及すらしていなかった。

AI が返す参照 URL は中継 URL（`vertexaisearch.cloud.google.com/grounding-api-redirect/...` 等）であることが多く、**一次情報そのものではない**。数値・日付・仕様・固有名詞を書いたら、URL を開いて現物を確認する。

## ブログ記事の場合

下書き後、次の 3 つを別々に投げる。まとめて聞くと指摘が浅くなる。

```bash
# 構成レビュー
{ echo "以下の記事の構成をレビューしてください。読者が離脱しそうな箇所と、順序を入れ替えるべき箇所を指摘してください。"; cat draft.md; } \
  | "${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high

# 推敲
{ echo "以下の記事を推敲してください。冗長な表現、係り受けの乱れ、不自然な敬体の揺れを、原文と修正案の対で示してください。"; cat draft.md; } \
  | "${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high

# タイトルとリード文
{ echo "以下の記事のタイトル案を5つと、リード文案を2つ出してください。煽らず内容に忠実にしてください。"; cat draft.md; } \
  | "${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high
```

## 技術記事で事実確認が要るとき

Gemini は Web 検索ができる。

```bash
"${CLAUDE_PLUGIN_ROOT}/skills/japanese-writing/scripts/gemini-ja.sh" -m gemini-3.1-pro-high \
  "Web 検索で <確認したい事実> を調べて。参照 URL も併記し、確証が得られない場合は「不明」と明記して"
```

**Gemini の回答は根拠ではない。** 併記された URL を自分で確認してから記事に書く。出典が複数になる調査記事では `hdd:source-traceability` に従い、一次情報（`S`）と二次情報（`M`）を分けて `sources.md` に集約する。

## Zenn 記事の制約

Zenn の `articles/` 配下に書く場合、`title` は **70 文字以内**。超えると公開時にエラーになる。frontmatter を検査する hook を入れている環境なら書き込み前に止まるが、入れていなくても、タイトルを考える時点で文字数を数えておく。

## プロンプトの型

- 役割ではなく**成果物の形**を指定する（「校正者として」ではなく「原文と修正案の対で」）
- 「1文だけ」「箇条書きで」など出力形式を明示する。指定がないと解説が付いてくる
- 事実確認では「参照 URL も併記して」「確証がなければ『不明』と明記して」を必ず付ける

## 注意

- Antigravity にサインイン済みであることが前提。認証が切れていれば Claude 単独で書き、その旨を伝える
- 実行のたびに Antigravity 側に会話が作られる。不要なものは適宜削除する
- Gemini CLI（`gemini` コマンド）は消費者向け提供が 2026-06-18 に終了しており、Google AI Pro / Ultra の枠では使えない。`agy` が公式の移行先
