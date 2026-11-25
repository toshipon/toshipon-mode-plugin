# Insight Discovery

日々の作業に残った摩擦・やり直し・失敗・気づきから、業務をまたぐ共通構造を探し、研究課題の候補を最大 3 件出します。候補には、観測事実、証拠 ID、疑う前提、反証可能な仮説、最小実験、成功条件と棄却条件が付きます。候補が無い週は「該当なし」を返します。

`autonomous-improvement` skill の runtime（キュー、ロック、追記専用の台帳、伏せ字、人の承認、スケジュール、agent adapter）の上に載っています。その skill が隣に無いと動きません。

| 部品 | 役割 |
|---|---|
| `insight-discovery` skill | 発見ループの手順 |
| `insight-discovery` agent | 観測、共通構造、仮説、最小実験の草案。ツールを持たない |
| `insight-auditor` agent | 別コンテキストでの独立監査。読むだけで、採否を決めない |
| `scripts/insight.py` | 取り込み・検査・振り分け・順位付け・状態遷移・記録を担う launcher |

skill と agent は判断を担い、script は決定論的な処理を担います。何を候補として残すか、だれに引き渡すか、どの順に並べるかは script が決めます。モデルの出力がそのまま状態を変えることはありません。

## 構成

```text
skills/insight-discovery/
  SKILL.md                      発見ループの手順
  README.md                     この文書
  references/                   各段の検査、隣の skill との境界
  scripts/insight.py            launcher（自分の位置から runtime と土台を解決する）
  runtime/insight_discovery/    foundation（土台の import はここだけ）/ state / evidence / signals
                                adapters / pipeline / checks / routing / ranking / decisions
                                report / jobs / cli
  schemas/                      evidence / insight / insight-audit / stages / ledger / insight-config
  config/insight.example.json   設定例（実際の設定は状態 home に置く）
  examples/synthetic/           合成データ（架空の 3 業務の記録、形式が崩れた入力、代役の agent）
  tests/                        unittest
agents/insight-discovery.md
agents/insight-auditor.md
```

## 準備

Python 3.10 以上が必要です。依存は `autonomous-improvement` と同じ `jsonschema` だけなので、同じ venv を使えます。

```bash
SKILLS=/path/to/plugins/toshipon-mode/skills
python3 -m venv ~/.venvs/ai-harness
~/.venvs/ai-harness/bin/pip install -r "$SKILLS/insight-discovery/requirements.txt"
alias insight="~/.venvs/ai-harness/bin/python $SKILLS/insight-discovery/scripts/insight.py"
```

コマンドは JSON を 1 つ出力します（`report` の Markdown を除く）。exit code は、0 が成功（該当なしを含む）、1 が進め方の gate の不合格（次の段ではない段を記録しようとした、など）、2 が入力・設定の誤り、3 が安全境界による拒否です。

## 合成データで試す

認証も課金も要りません。`demo` は合成データを repository の外へコピーし、モデルの代役（`stub_agent.py`）を adapter に設定します。

```bash
insight demo /tmp/insight-demo
export AI_HARNESS_HOME=/tmp/insight-demo/state

insight run --daily --dry-run     # 読む件数、次の行動、モデル呼び出しの見積もり。何も書かない
insight run --daily               # 取り込みから候補の登録まで
insight report                    # 観測事実と抽象化した問題だけが出る
insight prior --text "検査の位置が後ろすぎるのではないか"
insight report                    # 仮説、最小実験、成功・棄却条件が出る
insight weekly                    # 週次レポートを書く
insight select INS-20260105-01 --reviewer "<あなたの名前>" --reason "今週試す"
insight resolve INS-20260105-01 --status tested --reviewer "<あなたの名前>" --reason "やり直しが半減した"
```

候補 ID の日付は実行した日になります。合成データには、請求の突合バッチ、CI、設計文書のレビューという架空の 3 業務に「後の工程で初めて入力の不備が分かり、やり直す」記録を入れてあります。代役の agent は、件数から機械的に出した信号に沿って答えるだけです。この実行で確かめられるのは、検査・振り分け・順位付けの仕組みです。実際のモデルが共通構造を言い当てるかは、[制限と未検証事項](#制限と未検証事項)を見てください。

## 入力

読むのは、設定の `sources` に並べたものと、`ingest` で指定したものだけです。セッションの記録（`~/.claude/projects/`）は読みません。

| adapter | 読むもの | 本文 | 既定の区分 |
|---|---|---|---|
| `manual` | 手で書いた JSON / Markdown（メモ、制約、気づき、他のツールから書き出した仮説） | 読む（伏せ字のうえ保存） | ファイルに書いた `classification`。無ければ `internal` |
| `ledger` | 土台の hook 記録（ツール失敗、中断）と改善 run の台帳（棄却、人の介入、監査指摘の分類） | 読まない（件数と分類だけ） | `internal` |
| `claude_insights` | `/insights` が `~/.claude/usage-data/` に残した `facets/` と `session-meta/` の構造化項目 | 読まない | `internal` |
| `review` | `pr-feedback` command が残す `*_review_findings.md` | 指摘ごとに「問題:」の行。無ければ見出し（`missing` に `problem` が入る） | `internal` |

Markdown の手動エクスポートは次の形です。JSON の形は `schemas/stages.schema.json` の `manual_export` にあります。

```markdown
classification: synthetic
## billing-reconciliation | back-office
- [retry:rerun-after-late-check] 突合バッチを最後まで流してから入力の不備が分かり、再実行した
- [constraint] 入力の形式は取り込み側では検査していない
```

見出しは業務の key（半角英数字と `_.:-`）と、任意の分野です。項目の `[種類]` は friction、retry、failure、manual_intervention、constraint、review_finding、hypothesis_outcome、unexpected_success、user_note のどれかで、`:` の後ろに集計用の label を付けられます。同じ label が複数の業務に現れると、業務をまたぐ信号として数えられます。

「業務」の単位は adapter で違います。`manual` は見出しに書いた key、`ledger` は改善 run とプロジェクト、`claude_insights` は goal の分類、`review` は指摘ファイル（PR）です。`ledger` と `review` では、同じ仕事の中の run 2 回や PR 2 本も「2 つの業務」と数えられます。業務をまたぐ共通構造として扱いたい記録は、`manual` で業務の key を付けて渡すのが確実です。`loop-engineering` や hdd の仮説は、この形に書き出して読ませます。

### `/insights` の扱い

`/insights` の出力形式は公開されていません。Claude Code 2.1.330 のバイナリから読み取った保存先と項目名に合わせてあり、その依存は `adapters/claude_insights.py` の 1 ファイルに閉じています。

- この skill は `/insights` を起動しません。利用者が対話で実行した後に残るファイルを読みます。
- 読むのは `friction_counts`、`goal_categories`、`tool_error_categories`、`user_interruptions` の件数と名前だけです。`brief_summary`、`underlying_goal`、`first_prompt`、`summary`、`project_path` は読みません。`report.html` も解析しません。
- 項目名や型が違うファイルは、中身を推測せずに `format_unrecognized` として件数を返します。その場合は `manual` か `ledger` を入力にしてください。
- 業務の key は、そのセッションで最も多い `goal_categories` から `goal:<名前>` として作ります。`goal_categories` が無いセッションは `goal:unknown` にまとまり、証拠の `missing` に `goal_categories` が入ります。

### 秘密情報と業務データ

- 秘密情報の形をした文字列は、保存前に土台の検出器で伏せます。土台の設定 `privacy.deny_terms_file` に並べた語（顧客名、内部ホスト名など）は、本文ではマスクし、業務の key や label に含まれていれば hash か既定値に置き換えます。
- 上限（`max_file_bytes`、`max_text_chars`）を超えた入力は、切り詰めずに除外します。`ingest` でファイルを 1 つ指定した場合は exit 3 で拒否し、日次実行ではその入力を `refused` として報告して残りの入力を続けます。
- 区分が `llm_policy` で許可されていない証拠は、分析サイクルに入りません。件数は `next` と `status` の `withheld_by_llm_policy` に出ます。`..` を含むパスと symlink は拒否します。
- 証拠は区分（public / synthetic / internal / confidential）を持ちます。土台の設定 `llm_policy.allowed_classifications`（既定は public と synthetic）に無い区分の証拠は、モデルに渡しません。実際の作業記録（`internal`）を分析するには、利用者がこの設定に `internal` を足す必要があります。
- 状態は repository の外（既定 `~/.local/state/ai-harness/insight/`、0700 / 0600）に置きます。公開する前の検査には `insight scan <path>` を使えます。

## 発見ループ

1. 取り込み。証拠 ID を付け、重複を除き、件数から反復・摩擦・例外・予想外の成功の信号を出します（モデルを使いません）。
2. observe。観測事実と解釈を分けます。存在しない証拠 ID、原文に無い引用は落とします。
3. structure。異なる業務の観測に共通する形を群にし、個別の問題・構造的な原因・前提の 3 段で問い直します。1 つの業務にしか証拠が無い群は落とします。
4. hypothesize。前提の反転、類推、制約の除去・追加、反例探索で仮説を作ります。作り方が 1 種類だけの群、反証条件の無い仮説、棄却済みの言い換えは落とします。
5. audit。`insight-auditor` が別コンテキストで、既存手法で足りないか、問題設定が誤っていないか、反証可能かを評価します。監査は候補を外に出す向きにしか働きません。
6. assess。残った仮説に 4 軸の序数、最小実験、比較対象、成功条件、棄却条件、必要時間、確信度を付けます。
7. 順位付け。序数の合計と固定の tie-break で並べ、上位 3 件を候補として登録します。

各段の検査と落ちる理由の一覧は [references/discovery-loop.md](references/discovery-loop.md) にあります。

新規性は主張しません。既存手法を調べていない候補は「未調査」と表示します。調べた場合も「調べた範囲では見つからなかった（クエリ n 件、日付）」という形でだけ書きます。モデルの出力に「新規」「前例がない」などの断定があれば、その項目を落とします。

### 既存手法の調査

外部検索は、対話での手動実行のときだけ、実行ごとの承認付きで行います。スケジュール実行では検索しません。

```bash
insight search approve INS-20260105-01 --query "early validation rework reduction" \
  --reviewer "<あなたの名前>" --reason "既存手法を確認する"
# 承認された検索語で、対話のセッションが調べる
insight search record INS-20260105-01 --file found.json
```

`search approve` は、検索語に秘密情報、禁止語、業務の key、証拠の本文の引用が含まれていないこと、長さと件数が上限内であることを確かめます。通らなければ exit 3 で、検索語は記録も表示もしません。`search record` は、承認済みの検索語と一致すること、出典が認証情報を含まない https の URL であること、host が `search.allowed_domains` にあることを確かめます。既定は空なので、許可する公開情報源を設定に並べるまで、出典は記録できません。

## 週次の選択

`weekly` がレポートを書き、人が最大 1 件を選びます。

- レポートは、利用者が自分の仮説を `prior --text` で書くまで、観測事実と抽象化した問題だけを出します。書かずに進めるには `prior --skip --reason` で理由を残します。
- `select`、`resolve`、`handoff`、`abandon`、`search approve` は土台の人間ゲートを通ります。reviewer が AI や自動化の名前だと拒否します。これらと `prior`、`search record` は、スケジュール実行や harness が起動した子プロセスからは記録できません。`select` は週に 2 件目を拒否します（週は local time の ISO 週）。
- 自分の仮説は週に 1 回だけ書けます。書くか、書かずに進めるかを記録した後は、その週の `prior` は拒否します。生成された仮説を読んだ後で「自分の仮説」を足せないようにするためです。`report --format json` も同じ規則で欄を絞ります。
- 状態は candidate → accepted → tested / rejected と進みます。tested と rejected の後は変更できません。候補の実験案や成功・棄却条件を書き換えるコマンドはありません。
- 棄却した候補とほぼ同じ文面の仮説は、その根拠になる証拠が増えない限り、再び提示しません。

## 隣の skill との分担

成果物の改善は `autonomous-improvement`、作業手法の改善は `harness-evolution` が扱います。監査が「既存の手法で足りる」とした候補は、`<state home>/insight/handoffs/` のメモと台帳の記録として引き渡し、候補一覧から外します。引き渡し先への登録は人が行います。評価基準は読みも書きもしません。詳しくは [references/boundaries.md](references/boundaries.md) にあります。

## スケジュールに載せる

既定では何も自動起動しません。載せるかどうかは利用者が決めます。

土台の設定（`<state home>/config.json`）の `schedule` に、Insight の job を足します。時刻はその Mac の local time です。

```json
{
  "schedule": [
    { "kind": "evolve", "hour": 2, "minute": 0 },
    { "kind": "weekly_report", "weekday": 0, "hour": 9, "minute": 0 },
    { "kind": "insight_daily", "hour": 3, "minute": 0 },
    { "kind": "insight_weekly", "weekday": 0, "hour": 9, "minute": 30 }
  ]
}
```

そのうえで、定期的に起動するコマンドを `harness.py schedule-tick` から `insight.py schedule-tick` に替えます。`insight.py schedule-tick` は土台の tick をそのまま呼び、土台の job（audit、evolve、weekly_report）と Insight の job の両方を処理します。launchd の plist は `autonomous-improvement/integrations/launchd/` の例を使い、`ProgramArguments` の script だけを差し替えてください。`harness.py schedule-tick` のままにすると、Insight の job は失敗せずにキューに残り続けます。

```bash
insight schedule-tick --dry-run   # キューに入る予定の job と、Insight の日次実行の見積もり。何も書かない
insight schedule-tick             # 期限の来た job をキューに入れて処理する
insight status                    # 件数、次の行動、今週の選択、最後に上限で止まった段
```

`insight schedule-tick --dry-run` が試すのは Insight の分だけです。土台の job（audit、evolve、weekly_report）の予行は、これまでどおり `harness schedule-tick --dry-run` で行います。

- 日次（`insight_daily`）は、前回から証拠が増えたときだけ分析します。増えていなければモデルを呼ばずに終わります。分析には、業務をまたぐ照合のために、過去の証拠も `max_evidence_per_cycle` の範囲で含めます。
- 週次（`insight_weekly`）はレポートを書くだけです。選択は人が `select` で行います。
- 同じ枠の job は 1 回しかキューに入りません。スリープで何日か飛んでも、起動後に入るのは最新の枠の 1 件です。
- 上限（モデルの呼び出し回数、秒数、土台の 1 日の費用上限）に達すると `budget_exhausted` を記録して止まり、次の実行が続きから進みます。失敗した job は、土台のキューが backoff を挟んで再試行します。
- 同時に 2 つは走りません。実行中に手動で `run --daily` を重ねると、後から来た方は何もせずに終わります。job が重なった場合は、土台のキューが後で再試行します。
- cmux などの UI は `insight status` とレポートのファイルを読むだけです。この skill は UI も launchd も操作しません。

## 設定

`<state home>/insight/config.json` に置きます。項目はすべて省略でき、例は [config/insight.example.json](config/insight.example.json)、定義は `schemas/insight-config.schema.json` にあります。キュー、費用の上限、`llm_policy`、`privacy`、`schedule` は土台の `config.json` の値を使います。

| 項目 | 既定 | 意味 |
|---|---|---|
| `sources` | `ledger` と `claude_insights` | 日次実行が取り込む入力 |
| `limits.max_evidence_per_cycle` | 60 | 1 回の分析に入れる証拠の件数。超えた分は次の分析に回る |
| `limits.max_llm_calls_per_run` | 6 | 1 回の実行でモデルを呼ぶ回数の上限 |
| `limits.max_seconds_per_run` | 900 | 1 回の実行の時間の上限 |
| `gates.min_evidence` / `min_distinct_contexts` | 2 / 2 | 群が候補になるのに必要な証拠の件数と業務の数 |
| `gates.similarity` | 0.85 | 既出の仮説と同じとみなす文面の類似度 |
| `gates.max_candidates` | 3 | 提示する件数（3 が上限） |
| `agents.analyst` / `agents.auditor` | なし | 自動実行で使う adapter。形は土台の `agents` と同じ（`claude` か `argv`） |
| `search.allowed_domains` | 空 | 出典として記録できる host。空のままでは出典を記録できない |

adapter を設定しなくても、対話で `step prompt` / `step record` を使えば全段を進められます。`claude` adapter は土台の `agents.claude_argv` が組み立てるコマンドで起動され、ツール無し・MCP 無し・セッション保存無しで動きます。

## 状態の場所と削除

```text
<state home>/insight/
  config.json     設定
  ledger.jsonl    追記専用の台帳（証拠、各段の記録、候補、選択、引き渡し）。hash chain 付き
  handoffs/       引き渡しのメモ
  reports/        週次レポート
```

台帳とレポートは、土台の保持期間（`retention_days`）では自動削除されません。消すには `<state home>/insight/` を削除します。土台の `harness.py gc --all` は、土台の `config.json` だけを残して状態 home を消すので、`insight/` は設定ごと消えます。

## 制限と未検証事項

- 実際のモデルでの確認は、合成データを入力にした 2 回の実行だけです（全段を実モデルで 1 回、assess の段に届かせるために監査だけ代役にして 1 回）。3 業務にまたがる群は 2 回とも作られましたが、回数が少なく、発見の質を測ったとは言えません。CI は代役の agent で回しており、モデルの出力の質は測っていません。
- `/insights` の実ファイルでは確かめていません。開発した Mac では `/insights` を実行しておらず、`~/.claude/usage-data/` がありません。項目名はバイナリの文字列から読み取ったものです。
- `review` adapter は、`pr-feedback` の記録が `###` の見出しと「問題:」の行で書かれている場合だけ読めます。その記録の書式は厳密には決まっていません。
- 類似の判定は文面の一致度（difflib）です。言い換えた同じ仮説は通り抜けることがあります。
- 人間ゲートは手続き上の制御で、認証ではありません（土台と同じ）。承認の理由に書いた文は、秘密情報の形だけを伏せます。禁止語のマスクは掛かりません。
- 新規性の断定は語の一覧で検出しています。一覧に無い言い回しは通り抜けます。監査役の文章（根拠、指摘）には、この検査を掛けていません。
- モデルが形式に合わない出力を返し続けると、同じ段で止まり続けます。その場合は `abandon --reviewer --reason` で人がサイクルを閉じます。
- APM 経由で入れた場合の動作は確かめていません。隣の skill の runtime を相対パスで参照するので、`/plugin` での配布を前提にしています。
- 外部検索そのものは実装していません。承認と記録だけを持ち、検索は対話のセッションが行います。
