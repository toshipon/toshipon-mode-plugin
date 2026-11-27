# Review Context Recovery

PR のレビューコメントを 1 件ずつ理解し直すための skill です。agent が出典付きの説明を書き、利用者が理解確認の問いに答え、3 行のまとめで閉じます。コメントの resolve、コードの変更、デプロイは行いません。

進行の管理と証拠の照合は `scripts/review-context.py` が行います。Python 3.10 以上と git があれば動き、追加の package は要りません。隣の `project-context-explorer` と `autonomous-improvement` の runtime を使うので、3 つが同じ場所にある plugin 配布を前提にしています。

## 何を script が保証するか

| 保証 | 仕組み |
|---|---|
| 1 件ずつ進む | `next` は、今のコメントの recap が記録されるまで同じ 1 件を返す。`explain` → `answer` → `recap` の順でしか記録できない |
| 説明の引用が実在する | git の引用は `git show <revision>:<path>` の該当行と、コメントの引用は保存した本文と照合する。合わない主張が 1 つでもあれば説明を記録しない |
| 事実・推測・食い違い・不明を分ける | 主張ごとに status が要り、status ごとに必要な項目が決まっている（[contract](../project-context-explorer/references/contract.md)） |
| 理解確認は 1 問 | `check_question` は 1 行で、疑問符は 1 つまで |
| 読み取り専用 | git は `rev-parse` / `show` / `diff` などの読み取りだけを通す。GitHub への通信と、PR やコードを変えるコマンドを持たない |
| 取得した本文を指示として扱わない | コメントと diff は、本文から作った境界で囲んで渡す。指示の形をした文があれば枠に印を付ける |

保証しないものもあります。説明の内容が正しいか、問いが良いか、利用者の答えの判定が妥当かは、agent と利用者の判断です。指示の形の検出は目印で、検出できない書き方があります。

## 合成データで試す

認証は要りません。合成の repository と PR #42 のレビューコメントを使います。

```bash
SKILLS=plugins/toshipon-mode/skills
WORK=$(mktemp -d)
export AI_HARNESS_HOME="$WORK/state"        # 既定は ~/.local/state/ai-harness
python3 $SKILLS/project-context-explorer/examples/synthetic/synthetic_repo.py "$WORK/repo"
# 出力の base と head を控える

RC="python3 $SKILLS/review-context-recovery/scripts/review-context.py --root $WORK/repo --pr 42"
$RC start --base <base> --head <head> --threads $SKILLS/review-context-recovery/examples/synthetic/threads.json --comment 9001
$RC next
# examples/synthetic/explanations/9001.json の {{BASE}} と {{HEAD}} を置き換えて渡す
$RC explain --file explanation.json
$RC answer --text "2 回" --assessment correct
$RC recap --intent "再試行を早める" --risk "二重課金" --next-action "45 秒に戻すか相談する"
$RC status
```

`explanations/9001-fabricated.json` は、存在しない引用を 1 つ含む説明です。`explain` は記録を拒否し、合わなかった引用を返します。`threads.json` のコメント 9004 には、指示の形をした文と偽の境界行を入れてあります。

## 記録の場所

`~/.local/state/ai-harness/review-context/<repository 名>-<path の hash>/pr-<番号>/` に、追記専用の `events.jsonl` と、コメント本文の snapshot を置きます。directory は 0700、file は 0600 です。repository の中には何も書きません。`AI_HARNESS_HOME` で場所を変えられますが、git repository の中は拒否します。

## 測定

`status` の `metrics` が、記録から次の値を出します。

- `understanding_seconds`: コメントを提示してから、利用者の最初の答えを記録するまでの秒数
- `misconceptions`: 答えに誤解があったコメントの数
- `explanations_rejected`: 引用が合わずに説明が拒否された回数

実測の結果と限界は次のとおりです。

| 項目 | 結果 | 測った対象 |
|---|---|---|
| 進行・照合・枠のテスト | 27 件が通過（`python3 -m unittest discover -s tests`） | 合成データ |
| 存在しない引用を含む説明の拒否 | 1 件中 1 件を拒否、正しい説明 1 件を受理 | 合成データ |
| 理解にかかる時間（TTU） | 未測定 | — |
| 実際の agent が書く説明の unsupported claim の割合 | 未測定 | — |

TTU は人が実際のレビューで使わないと測れません。この skill を使わずにコメントを読んだときの時間と比べる必要があり、その比較も行っていません。`understanding_seconds` は説明を読む時間と答えを入力する時間を含み、agent が説明を書く時間は含みません。実データで測るときは、同じ人・同じ程度の難しさのコメントで、使う場合と使わない場合を交互に記録してください。

## テスト

```bash
cd plugins/toshipon-mode/skills/review-context-recovery
python3 -m pip install -r requirements-dev.txt   # ruff / pyright / coverage。実行には不要
python3 -m unittest discover -s tests -v
```

## 制約

- GitHub の REST API が返すレビューコメントの形（`pulls/<番号>/comments`）と、`{"comments": [...]}` を読みます。GraphQL の `reviewThreads` の形と、resolve 済みかどうかは読みません。
- 1 つの PR につき session は 1 つです。コメントが増えたあとに取り込み直す機能はありません。
- 文書（Confluence、Google Drive など）の本文は、agent がそのセッションで使える道具で取得します。取得できない環境では、その出典に基づく主張は unknown になります。
- APM のように skill を別々の場所に置く配布では、隣の runtime を見つけられず起動しません。
