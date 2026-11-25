# Autonomous Improvement Harness

成果物と、成果物を作る Harness 自体を、記録と測定に基づいて継続改善するための基盤です。2 つの skill と 2 つの agent、決定論的な runtime で構成します。

| 部品 | 役割 |
|---|---|
| `autonomous-improvement` skill | 成果物の改善ループ（仮説台帳、評価、回帰ガード、停滞検出、監査） |
| `harness-evolution` skill | Harness のメタ改善（失敗パターン、候補版、比較評価、提案、承認） |
| `improvement-auditor` agent | 別コンテキストでの独立監査。読むだけで、合格条件を変えない |
| `harness-evolution` agent | 改善仮説と候補版の作成。隔離 worktree の中だけを編集する |
| `scripts/harness.py` | 記録・測定・採否・状態遷移・キュー・スケジュールを担う launcher |

skill は判断の手順を定め、script は決定論的な処理を担います。採否、停滞、終了条件、公開の可否は script が台帳から計算し、agent の自己申告では決まりません。

[isucon2026 の公開リポジトリ](https://github.com/luvtechno/isucon2026)にある、実験台帳・評価・監査を残す進め方を参考にしています。そこで使われた非公開の Harness を再現したものではありません。

## 構成

```text
skills/autonomous-improvement/
  SKILL.md                     改善ループの手順
  README.md                    この文書
  scripts/harness.py           launcher（自分の位置から runtime と schema を解決する）
  runtime/ai_harness/          store / redact / evaluation / guard / loop / jobs / record
                               agents / evolve / dispatch / status / cli
  schemas/                     event / ledger / evaluation / proposal / spec / audit
                               evolution / config
  config/harness.example.json  設定例（実際の設定は状態 home に置く）
  integrations/claude-code/    hooks.example.json
  integrations/launchd/        *.plist.example
  examples/synthetic/          合成データ（改善対象、評価器、評価 suite、adapter の代役）
  tests/                       unittest
skills/harness-evolution/SKILL.md
agents/improvement-auditor.md
agents/harness-evolution.md
```

設定は JSON です。Python の標準ライブラリに YAML parser が無く、依存を `jsonschema` だけに保つためです。

## 準備

Python 3.10 以上と git が必要です。依存は利用側の venv に入れます。plugin cache は書き換えません。

```bash
SKILL=/path/to/plugins/toshipon-mode/skills/autonomous-improvement
python3 -m venv ~/.venvs/ai-harness
~/.venvs/ai-harness/bin/pip install -r "$SKILL/requirements.txt"
alias harness="~/.venvs/ai-harness/bin/python $SKILL/scripts/harness.py"
```

すべてのコマンドは JSON を 1 つ出力します。拒否は exit 2 で、理由を stderr に JSON で出します。

## 合成データで試す

認証も課金も要りません。`demo` は合成サンプルを repository の外へコピーし、改善対象と Harness の代役を git repository にします。

```bash
harness demo /tmp/ai-harness-demo
export AI_HARNESS_HOME=/tmp/ai-harness-demo/state
```

### 改善ループ

```bash
D=/tmp/ai-harness-demo
harness run init --project $D/project --spec $D/spec.json --run-id demo
harness run baseline demo
harness run next demo                                  # hypothesize
harness hypothesis add demo --file $D/hypotheses/h1.json
harness experiment start demo H-0001
sed -i.bak 's/"cache_entries": 10/"cache_entries": 25/' $D/project/settings.json && rm $D/project/settings.json.bak
harness experiment finish demo H-0001                  # adopt、次は audit（completion）
echo '{"findings": [], "limitations": ["合成データのみ"]}' > $D/audit.json
harness audit record demo --file $D/audit.json --independence subagent
harness run stop demo --reason passed
harness run report demo
```

`tests_pass` を false にする変更は回帰として棄却され、`settings.json` だけが元に戻ります。`evaluate.py` を書き換える変更は `criteria_tamper` で、何も変更しなかった実験は `no_change` で棄却されます。

### メタ改善と、スケジュール起動の dry-run

```bash
harness evolve suite-approve --file $D/evolution/suite.json --reviewer "<あなたの名前>" --reason "合成ケースを確認した"
for i in 0 1 2; do
  echo "{\"hook_event_name\":\"PostToolUseFailure\",\"session_id\":\"s$i\",\"cwd\":\"/work/p$((i % 2))\",\"tool_name\":\"Bash\",\"tool_use_id\":\"t$i\"}" | harness record
done
harness evolve analyze                 # tool_failure:Bash が recurring になる
harness schedule-tick --dry-run        # 写しの上で分析→仮説→候補→比較→監査→提案まで通す。実状態は変わらない
harness schedule-tick                  # 実際に提案 P-0001 を作る
harness status
harness evolve publish-check P-0001    # 承認が無いので拒否される
harness evolve approve P-0001 --reviewer "<あなたの名前>" --reason "差分と測定を読んだ"
harness evolve publish-check P-0001    # ブランチと PR を作るコマンドを表示するだけ
harness evolve cleanup && harness gc --all
```

合成サンプルの adapter（`hypothesize.py`、`candidate.py`、`auditor.py`）と runner は、決定論的なローカル script です。agent の代役であり、モデルは呼びません。hook の記録は実作業から来たものとして `internal` に分類されるので、demo の設定は `llm_policy.allowed_classifications` に `internal` を足しています。

## 改善ループ

手順は [SKILL.md](SKILL.md) にあります。ここでは仕組みを補います。

- **台帳**: `runs/<run id>/ledger.jsonl`。追記専用で、各行が前の行の hash を持ちます。行の編集や破損があると以後のコマンドが拒否されます。途中で切れた最終行（書き込み中の crash）は無視され、次の追記で取り除かれます。
- **仮説台帳の項目**: 仮説 ID、根拠、反証条件、変更差分（`patches/<ID>.patch` と sha256）、検証コマンド、測定値、採否、理由、再現情報（実験前の HEAD と criteria hash）。棄却・評価不能・人の判断待ちも同じ形で残ります。
- **測定誤差**: scored 指標の変化は、`max(min_effect × 基準値, noise_floor, noise_k × 差の標準誤差)` を超えたときだけ効果として扱います。基準値が 0 のとき、`min_effect` は絶対値の変化として読みます。`evaluation.repeats` か評価器の `samples` でばらつきを測れます。
- **回帰ガード**: 実験前に未コミットだったファイルは利用者のものとして扱い、触れた実験は `needs_human` で止めます（差し戻しません）。差し戻すのは、実験前に clean だったファイルと、実験が新しく作ったファイルだけです。パスは glob として解釈しません。人が `run unblock` を記録すると、残った変更が次の実験の手柄にならないよう、ベースラインを取り直します。
- **評価基準の固定**: 指標、回帰チェック、合格条件、評価器ファイルの内容を criteria hash に固定します。実験が評価器に触れると棄却、実験の外で変わっていると実行を拒否します。変更は `run criteria-change --reviewer <人>` だけで、ベースラインを取り直します。spec に秘密情報の形をした文字列があると `run init` を拒否します。認証情報は環境変数で評価器に渡します。
- **停滞**: 既定は「同じ失敗理由 3 回」または「採用の無い試行が連続 3 回」。`plateau.same_failure`、`plateau.no_improvement` で変えられます。評価不能の試行は数えません。監査の後にもう一度停滞したら `no_improvement` で終了します。

## メタ改善

手順は [harness-evolution の SKILL.md](../harness-evolution/SKILL.md) にあります。

- **失敗パターン**: 改善ループの棄却理由、確認済みの監査指摘、hook が記録したツール失敗を signature ごとに数えます。confidence は `candidate`（一般化しない）、`recurring`（既定で 3 件以上・出所 2 つ以上）、`strong` の 3 段階です。パターンは出所のデータ分類を引き継ぎます。改善ループ由来は spec の分類、hook 由来は `internal` です。
- **比較**: 現行版は登録時の commit に固定し、候補版は状態 home の中の git worktree に作ります。runner とケースは suite のディレクトリの中に置き、suite は対象 repository の外に置きます（中にあると承認を拒否します）。候補版からは runner も holdout ケースも見えず、書き換えても測定には影響しません。
- **測るもの**: 初回成功率、失敗再発率（過去に失敗したケースの失敗率）、修正回数、実行時間、トークン、費用、品質。試行ごとの値から平均・標準偏差・試行回数を出します。試行 1 回のときは、ばらつきが不明であることを報告に書きます。
- **提案の条件**: 必須条件の回帰ゼロ、holdout の改善が事前に決めた幅と誤差を超えること、独立監査に refuted でない重大指摘が残っていないこと。どれかが欠ければ保留か棄却です。

## 自動起動（オプトイン）

既定では何も常駐せず、hook も登録されません。有効にするのは利用者です。

役割分担は次のとおりです。

| 役割 | 起動するもの | すること |
|---|---|---|
| Worker | 日常の Claude Code 作業 | hook が 1 行の記録を書き、必要なら監査 job をキューに入れる。モデルも他のプロセスも起動しない |
| Auditor | キューの `audit` job、または対話中の subagent | 停滞か完了を迎えた改善ループを独立に監査する |
| Evolution | キューの `evolve` job（1 日 1 回） | 失敗パターンを分析する。繰り返しが無ければ adapter を起動せずに終わる |
| Scheduler | launchd が `schedule-tick` を起動 | 予定時刻の job をキューに入れ、キューを 1 回処理して終了する |

cmux などの端末は対話作業と観測に使います。スケジュールは cmux に依存しません。

### hook を有効にする

`integrations/claude-code/hooks.example.json` の `hooks` を、パスを書き換えて `~/.claude/settings.json` の `hooks` に足します。`Stop` と `PostToolUseFailure` で `harness.py record` を呼びます。

- 記録するのは、種別、時刻、作業ディレクトリの hash、session ID の hash、ツール名、中断かどうかだけです。プロンプト、ツールの入力、出力、パスは保存しません。
- `record` は必ず exit 0 で何も出力しません。記録に失敗しても作業は止まりません。
- Harness が起動した子プロセス（評価器、adapter、`claude -p`）には `AI_HARNESS_ACTIVE=1` が付き、その中で発火した hook は何も記録しません。hook から hook が連鎖しません。
- 無効にするには、足した項目を `settings.json` から消します。

### launchd に登録する

`integrations/launchd/com.example.ai-harness.schedule.plist.example` を複製し、Label とパスを書き換えます。

```bash
mkdir -p ~/.local/state/ai-harness/logs
cp "$SKILL/integrations/launchd/com.example.ai-harness.schedule.plist.example" \
   ~/Library/LaunchAgents/com.<you>.ai-harness.schedule.plist
# パスを編集してから
plutil -lint ~/Library/LaunchAgents/com.<you>.ai-harness.schedule.plist
harness schedule-tick --dry-run                                   # 登録前に、何が起動するかを確かめる
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.<you>.ai-harness.schedule.plist
launchctl print gui/$(id -u)/com.<you>.ai-harness.schedule        # 状態の確認
```

解除は次のとおりです。

```bash
launchctl bootout gui/$(id -u)/com.<you>.ai-harness.schedule
rm ~/Library/LaunchAgents/com.<you>.ai-harness.schedule.plist
```

既定の予定は、毎日 02:00（ローカル時刻）に `evolve`、毎週月曜 09:00 に `weekly_report` です。設定の `weekday` は 0 が月曜、launchd の `Weekday` は 1 が月曜です。両方を合わせて変えます。

スリープ中や電源断の間は実行されません。`schedule-tick` は起動されるたびに「直近の予定時刻」の job を 1 つだけキューに入れるので、数日空いても溜まった分をまとめて実行しません。plist の `RunAtLoad` により、ログイン時にも 1 回起動して取りこぼしを回収します。

### キュー

`queue/jobs.json` に永続化します。

| 性質 | 実装 |
|---|---|
| 重複排除 | job ID（`evolve:2026-01-08T02:00`、`audit:<run>:<台帳 seq>`）が既にあれば入れない |
| 排他 | 状態の書き換えは flock。`dispatch` は同時に 1 つだけ動き、2 つ目は何もせず終わる |
| timeout | adapter と runner に `timeout_seconds`。job には lease（既定 30 分） |
| retry | 失敗は指数 backoff（60 秒から、上限 1 時間）で既定 3 回まで |
| 失敗キュー | 上限を超えた job は `dead`。`status` と週次集計に出る。`queue retry <id>` で戻せる |
| 再開 | crash で `running` のまま残った job は、lease が切れたら失敗 1 回として再試行される |
| cancel | `queue cancel <id>`。実行中の job は区切りで止まり `cancelled` になる |
| 予算 | 1 回の dispatch の job 数・秒数、1 日の費用上限。上限に達したら agent を使う job を先送りする |
| dry-run | `dispatch --dry-run` / `schedule-tick --dry-run` |

### 自動実行時の権限

adapter は `agents.<役割>` に設定します。ローカルコマンド（`argv`）か、Claude Code CLI（`claude`）です。CLI の引数は `runtime/ai_harness/agents.py` の 1 か所で組み立てます。

- `--restricted --safe-mode`、`--strict-mcp-config` と空の MCP、`--permission-prompts none`、`--no-session-persistence`、`--disable-slash-commands`、`--max-budget-usd` を付けます。shell、ネットワーク、MCP、hook、plugin は使えません。`max_budget_usd` は必須で、呼び出しの前に 1 日の上限の残りと照合します。失敗した呼び出しも、返された費用（読めなければ上限額）を計上します。
- 監査と仮説はツール無し（`--tools ""`）、候補作成は `Read,Glob,Grep,Edit,Write` を worktree の中だけで使います。
- `--dangerously-skip-permissions`、`--allow-dangerously-skip-permissions`、`bypassPermissions` を含むコマンドは、評価器・回帰チェック・adapter・runner のどれであっても起動前に拒否します。`claude` を直接呼ぶコマンドでは `--settings`、`--allowedTools`、`--permission-prompt-tool`、`--permission-mode auto` も拒否します。
- adapter と suite の runner に渡す環境変数は `PATH`、`HOME`、locale、`ANTHROPIC_*`、`CLAUDE_*`、`AI_HARNESS_*` などに絞ります。改善ループの評価器と回帰チェックは利用者自身のコマンドなので、環境をそのまま引き継ぎます。
- Harness が呼ぶ git は、repository の hook、fsmonitor、外部 diff、textconv を実行しません。候補版の worktree では、`.git` が作成時の指し先のままであることを確かめてから git を呼びます。書き換えられていれば候補を拒否します。
- adapter の stderr は保存しません。入力の抜粋や認証情報が含まれうるためです。
- 入力はデータ分類（spec と suite の `data_classification`、既定 `internal`）を持ち、`llm_policy.allowed_classifications`（既定 `public`、`synthetic`）に無い分類は adapter に渡しません。payload は、元になった記録と suite のうち最も厳しい分類で判定します。実作業の記録を自動で監査・分析させるには、利用者が `internal` を明示的に足す必要があります。

## 設定

`config/harness.example.json` を `<状態 home>/config.json` に複製して編集します。すべての項目は省略でき、省略時は例と同じ既定値です（`agents`、`publish.allow_paths`、`privacy` は既定で空）。

| 項目 | 意味 |
|---|---|
| `collect.raw` | ツール失敗のエラー文（伏せ字・500 字まで）を記録するか。既定 false |
| `retention_days` | `gc` が消す基準。既定 30 日 |
| `queue.*` | retry 回数、backoff、lease |
| `budget.*` | 1 回の dispatch の job 数・秒数、1 日の費用上限 |
| `schedule` | `kind`（`evolve` / `weekly_report`）、`hour`、`minute`、任意で `weekday` |
| `evolution.*` | パターンを対象にする件数と出所の数 |
| `llm_policy.allowed_classifications` | adapter に渡してよいデータ分類 |
| `agents.*` | 役割ごとの adapter。`claude` には `max_budget_usd` が必須 |
| `publish.allow_paths` | 公開候補が触れてよいパスの glob。`*` と `?` は `/` をまたがず、`**` だけがまたぐ。空なら何も公開できない |
| `privacy.deny_terms_file` | 禁止語（顧客名、内部ホスト名など）を 1 行 1 語で書いたローカルファイル |

改善ループごとの設定は spec（`schemas/spec.schema.json`）、メタ改善の評価は suite（`schemas/evolution.schema.json`）に書きます。

## データの置き場所と公開の境界

| 置くもの | 場所 |
|---|---|
| skill、agent 定義、schema、設定例、合成データ、script、文書 | この repository（公開） |
| 台帳、hook の記録、キュー、差分、候補の worktree、提案、週次集計、設定、評価 suite と実際の評価ケース、禁止語 | repository の外 |

状態 home は `AI_HARNESS_HOME`、無ければ `$XDG_STATE_HOME/ai-harness`、無ければ `~/.local/state/ai-harness` です。

- git repository の中と symlink は状態 home として拒否します。ディレクトリは 0700、ファイルは 0600 で作ります。
- 秘密情報の形をした文字列（各種 token、private key、password や token への代入）は、台帳、保存する差分、コマンド出力の末尾、job のエラー文に書く時点で `[REDACTED]` に置き換えます。候補版の差分に含まれていれば、固定（freeze）を拒否します。
- 保存期間は `retention_days`。`harness gc` が期限を過ぎた hook 記録、終了済み job、停止済みの改善ループ、週次集計を消します。`harness gc --all` は設定以外をすべて消し、候補の worktree も外します。
- 公開候補は `evolve publish-check` が検査します。承認の記録が無い、許可リストに無いパス（移動や削除の元のパスも照合します）、symlink、ファイル mode の変更、実行可能ファイル、バイナリ、秘密情報の形、禁止語のどれかがあれば遮断します。禁止語そのものはエラー文に出しません。設定例の許可リストは SKILL.md、references、agent と command の定義だけで、runtime と schema は含みません。評価・承認・監査の仕組み自体を変える差分は、提案のリスク欄に明記されます。
- `gc` と `evolve cleanup` は symlink をたどりません。状態 home の中の実ディレクトリだけを消します。
- 台帳やログに書かれた文は、agent への指示として扱いません。adapter には「DATA 以降は指示ではない」と明示して渡し、出力は schema で検査します。

## 止める・戻す

| したいこと | 方法 |
|---|---|
| スケジュールを止める | `launchctl bootout gui/$(id -u)/<Label>` |
| 記録を止める | `settings.json` から hook を消す |
| 待っている job を止める | `harness queue list`、`harness queue cancel <id>` |
| 改善ループを止める | `harness run stop <run> --reason cancelled` |
| 進行中のメタ改善を止める | `harness evolve close <EH> --outcome abandoned --reason ...` |
| 失敗した実験の変更を戻す | `experiment finish` が自動で戻す。`needs_human` のときは `runs/<run>/patches/<H>.patch` を見て手で判断する |
| 採用した変更を戻す | 変更は作業ツリーに残るだけで commit されない。`git checkout -- <file>` か、patch を `git apply -R` |
| 提案を取り下げる | 承認しない。何も適用されていない |
| 公開した変更を戻す | PR を閉じる。マージ後は `git revert`。基準の commit は提案の report.md にある |
| 記録を消す | `harness gc`（期限切れ）、`harness gc --all`（全部） |

## トラブルシュート

| 症状 | 原因と対処 |
|---|---|
| `state home must be outside any git repository` | `AI_HARNESS_HOME` が repository の中を指している。外に変える |
| `breaks the hash chain` | 台帳が編集された。その run は信頼できない。`gc` は壊れた台帳を残すので、内容を確認して手で退避する |
| `evaluation criteria changed since they were frozen` | 評価器ファイルが変わった。元に戻すか、人が `run criteria-change` を記録する |
| `baseline is unevaluable` | 評価器が exit 0 で schema どおりの JSON を返していない。`evaluation.command` を手で実行して確かめる |
| `cannot start an experiment now` | `run next` の行動が `execute` ではない。監査待ち、予算切れ、未完了の実験のどれか |
| `duplicate of rejected H-...` | 棄却済みとほぼ同じ仮説。`new_evidence` を書くか、別の系統を試す |
| `the evaluation suite changed since it was approved` | suite、runner、ケースのどれかが変わった。意図した変更なら人が再承認する |
| `data classified internal may not be sent` | `llm_policy.allowed_classifications` に無い分類。意図して許可するなら設定に足す |
| job が `dead` | `harness status` の `dead_jobs` に最後のエラーがある。直してから `harness queue retry <id>` |
| `schedule-tick` が何もしない | 同じ予定時刻の job は 1 回だけ。`harness queue list` で確かめる。別の dispatch が動いていれば `skipped` を返す |
| hook を入れたのに記録が増えない | hook は失敗を表に出さない。`echo '{"hook_event_name":"Stop","session_id":"x","cwd":"/tmp"}' \| harness record` の後で `events.jsonl` を見る |

## 他の skill との関係

- `loop-engineering` は、プロダクトの仮説をマージ・デプロイ・本番計測で検証するループです。この Harness はマージもデプロイもしません。「tick は台帳から次の行動を決める純関数」という考え方だけを共有します。
- `verification-loop` は PR 前の検証です。改善ループの `guard.checks` に、その repository の検証コマンドを書けます。
- `nfr-design` の runtime とは独立です。道具立て（Python 3.10+、jsonschema、ruff、pyright、unittest）と、adapter の出力を schema で検査する方針を合わせています。
- レビュー指摘の学習や仕様の整合性検査のような別の harness を足すときの連携点は 3 つです。record の共通項目（`schema_version`、`id`、`run_id`、`created_at`）、承認者に AI の名前を受け付けない規則、confidence の語彙（`candidate` / `recurring` / `strong`、`toshipon-mode` の review-bot-triage と同じ）です。失敗の出所は `evolve.occurrences` に足せます。改善候補の承認は proposal と同じ流れに載せられます。この repository にそうした harness の実体はまだ無いので、連携点の設計だけです。

## テスト

```bash
python -m pip install -r requirements-dev.txt
python -m unittest discover -s tests -v
ruff check . && ruff format --check . && pyright
```

テストは合成データだけを使い、モデルもネットワークも呼びません。

## 制限と未検証の事項

- hook の入力項目と `settings.json` の書式は、Claude Code の公式資料（hooks reference）に基づきます。Claude Code から実際に hook が発火する経路は、この repository のテストでは確かめていません。
- Claude Code CLI の引数は、導入済みの CLI の `--help` に全て存在することをテストで確かめます（CLI が無い環境では skip）。実際に `claude -p` を起動した往復と、`--restricted` と `--safe-mode` の併用時の挙動は未検証です。
- launchd への登録と、スリープ復帰後の起動は未検証です。plist の構文と内容だけをテストします。
- 人の承認は手続き上の統制で、認証ではありません。AI や自動化の名前、自動実行中のプロセスからの記録を拒否しますが、人の名前を騙る入力や、環境変数を外した子プロセスからの記録は防げません。
- 台帳の hash chain に鍵はありません。検出できるのは偶発的な編集と破損です。状態 home に書ける同じ利用者が hash を計算し直して書き換えた場合は検出できません。`proposal.json` と patch も同じです。
- 許可回避オプションの拒否は、コマンドの引数に対して働きます。script の内部で付けた場合は検出できません。
- 秘密情報の検出は既知の形に対する正規表現です。形の決まっていない秘密（裸の AWS secret key、独自形式の token）は検出できません。禁止語ファイルで補ってください。
- 回帰ガードが見るのは git が報告する変更です。gitignore されたファイルへの変更は、評価器ファイルとして列挙したもの以外は検出も差し戻しもできません。
- OS レベルの sandbox はありません。runner と `argv` の adapter は利用者と同じ権限で動くので、状態 home を読み書きできます。候補版が書いた内容を実行する runner を使う場合は、runner の側で隔離してください。`claude` の adapter がファイルを worktree の外で読めないことは、CLI の `--restricted` に依存しています（実起動は未検証）。
- dry-run は状態 home の写しの上で動きますが、候補版と現行版の worktree は対象 repository に一時的に登録され、終了時に外されます。状態 home の中の symlink は写しません。
- 比較評価の統計は、試行間の平均の差と標準誤差だけです。試行回数とケース数の下限は suite で決めます。
- 候補版の隔離は git worktree です。対象が git repository でない場合は使えません。
- adapter に timeout はありますが、孫プロセスまでは止めません。
