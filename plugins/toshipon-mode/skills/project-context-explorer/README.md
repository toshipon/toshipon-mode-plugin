# Project Context Explorer

大きなプロジェクトに戻ってきたとき、資料をすべて読み込まずに理解し直すための skill です。entity の metadata だけを索引にし、問いに関係する範囲の原資料だけを取りに行き、主張を出典と revision に照合します。新しいセッションに渡す context packet と、ローカルで開く HTML explorer も作ります。書き込みとデプロイは行いません。

Python 3.10 以上と git があれば動き、追加の package は要りません。秘密情報の形の定義を隣の `autonomous-improvement` の runtime から読むので、同じ場所にある plugin 配布を前提にしています。`review-context-recovery` は、この skill の runtime にある主張の照合・snapshot・読み取り専用 git・untrusted の枠を使います。

## 何を script が行い、何を agent が行うか

| script（決定論的） | agent（判断） |
|---|---|
| entity の形の検査、git の出典の revision と hash の記録 | 何を entity にするか、説明をどう書くか |
| 関係の探索と、hop・件数・token の上限 | 範囲を広げるかどうか |
| 欠落、古い参照、食い違いの検出（規則だけ。埋めない、決着させない） | 原資料の取得（git、`gh`、接続済みの MCP） |
| 引用が出典のその revision にあるかの照合 | 主張を書く。status を選ぶ |
| context packet と HTML の生成、秘密情報の伏せ字、エスケープ | 答えを短く書く。学習用の問いを作る |
| 学習の記録と弱いところの集計 | 答えの判定と解説 |

データの形は [references/contract.md](references/contract.md) にあります。

## 合成データで試す

認証は要りません。合成の小さなプロジェクト（queue と worker、ADR 2 本、要求、議事録）を git repository にして使います。

```bash
SKILL=plugins/toshipon-mode/skills/project-context-explorer
WORK=$(mktemp -d)
export AI_HARNESS_HOME="$WORK/state"          # 既定は ~/.local/state/ai-harness
python3 $SKILL/examples/synthetic/synthetic_repo.py "$WORK/repo"

CE="python3 $SKILL/scripts/context-explorer.py --root $WORK/repo"
$CE sources --file $SKILL/examples/synthetic/sources.json
$CE index add --file $SKILL/examples/synthetic/entities.json
$CE check
$CE overview
$CE explore --question "orders queue の保持期間はなぜその値なのか"
$CE packet --entity PROP-retention --question "なぜ 14 日か"
$CE html                                       # 出力された path を browser で開く
```

合成データには次を入れてあります。

| 入れたもの | どこに出るか |
|---|---|
| 承認済みの決定が 7 日と 14 日で食い違う（DEC-0001 / DEC-0002） | `check` の `conflict` |
| 理由がどこにも書かれていない設定値（visibility timeout） | `check` の `missing_rationale` |
| 索引を作ったあとに変わる行（`synthetic_repo.py` は 2 commit を作る。base で索引を作ると head で古くなる） | `check` の `stale_reference` |
| 索引に無い entity への関係、届かない文書ツール | `dangling_reference`、`source_unavailable` |
| 議事録と entity の説明に紛れた指示文、HTML と script の断片 | prompt の枠の中、HTML の文字列 |
| 存在しない引用、revision 違い、行の指定違い、未取得の文書を引く主張 | `claims check` の unsupported |

## 記録の場所

`~/.local/state/ai-harness/context-explorer/<root 名>-<path の hash>/<scope>/` に、`index.json`、取得した本文の snapshot（`evidence/`）、学習の記録（`learning.jsonl`）、`explorer.html` を置きます。directory は 0700、file は 0600 です。`--out` で場所を指定できますが、git repository の中は拒否します。成果物にはプロジェクトの内部情報が入るので、共有する前に中身を確かめてください。

## HTML explorer の安全性

- entity の文字列は JSON として埋め込み、`<` `>` `&` を `<` などに置き換えます。画面には `textContent` で置き、markup として解釈する API（`innerHTML` など）を使いません。
- Content-Security-Policy は `default-src 'none'` で、同梱の script と style だけを hash で許可します。画像、通信、form の送信先、base URL は禁止です。外部の asset は読み込みません。
- 静的な HTML は MCP を呼べません。資料の取得は、コピーした context prompt を貼った agent のセッションが行います。HTML から直接取得する local bridge は実装していません。作る場合は、認可、送信先、保存の設計が先に要ります。

## 測定

`examples/synthetic/measure.py` が、人がいなくても測れる 2 つを合成データで測ります。

```bash
python3 plugins/toshipon-mode/skills/project-context-explorer/examples/synthetic/measure.py
```

実測の結果です（2026-11-27、Python 3.13、合成データ、1 回の実行で結果は決定論的）。

| 項目 | 結果 | 測った対象と方法 |
|---|---|---|
| Retrieval precision | 0.54（24 件中 13 件） | 合成データの 4 つの問い。`explore` の既定の上限（1 hop、12 件）で返った entity を、手作業のラベル（`gold.json`）と比べた |
| Retrieval recall | 0.93（14 件中 13 件） | 同上 |
| 仕込んだ unsupported claim の検出 | 4 件中 4 件 | `claims.json` の手作業のラベルと `claims check` の判定を比べた |
| 正しい主張の誤った拒否 | 6 件中 0 件 | 同上 |
| 探索が返す量 | 2000 entity の索引でも 5 件、6000 文字未満 | `tests/test_retrieval.py` |
| HTML の描画 | payload は実行されず、構成図と詳細が出る | headless Chrome（`--dump-dom` と screenshot）で 1 度だけ手で確かめた。CI では動かしていない |
| 理解にかかる時間（TTU） | 未測定 | — |
| 実際の agent が書く主張の unsupported claim rate | 未測定 | — |
| 実プロジェクトでの retrieval precision | 未測定 | — |

読み方と限界です。

- precision が 0.54 にとどまるのは、問いに合った上位 3 件の entity それぞれについて、1 hop 先をすべて返すからです。関係があるだけで問いに関係しない entity が入ります。返るのは metadata（1 件 240 文字まで）で、本文の取得は token の予算で別に絞られます。
- ラベルは実装した本人が、探索の結果を見る前に付けました。問いは 4 つ、entity は 15 件です。別の人のラベル、実プロジェクトの規模と書き方では測っていません。
- unsupported claim の検出は、引用が出典に実在するかの照合が、仕込んだ誤りを拾えるかを見たものです。agent が実際に書く主張のうち何割が unsupported になるか、ではありません。照合は、主張の文が引用から正しく導かれているかも見ません。
- TTU は人が実際に使わないと測れず、使わない場合との比較も要ります。`learn weak` の正答率と、`review-context-recovery` の `understanding_seconds` が、記録から出せる材料です。
- Confluence と Google Drive の MCP につないだ取得は試していません。取得手順はツール名を持たず、agent がセッションの中で道具を探す作りです。

## テスト

```bash
cd plugins/toshipon-mode/skills/project-context-explorer
python3 -m pip install -r requirements-dev.txt   # ruff / pyright / coverage。実行には不要
python3 -m unittest discover -s tests -v
```

テストは一時 directory に合成の git repository を作ります。git が要ります。

## 制約

- 索引は自動では作られません。entity を選んで書くのは agent で、索引の質がそのまま探索の質になります。
- 問いと entity の照合は、語と日本語の 2 文字の重なりを数えるだけです。言い換えや同義語は拾いません。
- git 以外の出典（PR、文書）が古くなったかどうかは検査しません。
- 行の指定がある出典は、上に行が挿入されるだけでも `stale_reference` になります。
- 指示の形をした文の検出は目印で、検出できない書き方があります。
- APM のように skill を別々の場所に置く配布では、隣の runtime を見つけられず起動しません。
