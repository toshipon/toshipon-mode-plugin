# Record schema

ループが読み書きする 4 種類のファイルの形。`loop.yaml` と `metrics.yaml` は repo の人間が書き、
ループは読むだけである。

`loop.yaml` と `metrics.yaml` が flat なのは意図である。launchd は bare な PATH でスクリプトを
起動するので、YAML parser が PATH に無い。scripts は `sed` と `awk` でこの 2 つを読む。ネストを
足すと parser が要り、parser は環境依存になる。

## 1. 仮説 record

`<records_dir>/hypotheses/PH-NNNN.yaml`。1 ファイル 1 仮説。トップレベルは flat か 1 段のみ。

```yaml
id: PH-0001
title: dashboard の strategies 取得が遅く、初回描画までに人が待たされる
state: drafted           # drafted|building|shipped|measuring|validated|invalidated|inconclusive|abandoned
priority: 2              # 小さいほど先。BUILD はこの順で 1 件選ぶ
metric: avg_ms:/api/strategies    # metrics.yaml にある key。無い key はテストが落とす
created_at: 2026-09-28
updated_at: 2026-09-28

claim: >
  /api/strategies が毎回 mark price を引き直しているので遅い。キャッシュすると平均応答時間が縮む。
claims: product
not_evidence_for: trading performance (Sharpe, PnL, DSR, win rate)

origin_kind: interview   # interview|owner|incident
origin_ref: e6c83f59-…  # 元になった interview の id（記録面の id）。owner の時は空でよい

falsification: measurement.value が baseline.value の 95% を上回ったままである
success: measurement.value が baseline.value の 70% 以下で、n が cap_min_sample 以上ある

baseline:
  value: null
  n: null
  window: {start: null, end: null}   # end はデプロイ日の前日
  deploy_version: null               # 変更前の版。measurement と同一なら判定は拒否される
  query_id: null                     # loop-metrics.sh の sha7
  captured_at: null

power:
  expected_n_per_day: null
  horizon_days: 7
  decidable: null         # false なら building に進めず abandoned
  note: null              # なぜ検出できる、またはできないかを 1 文で

change_summary: strategies の mark price を 60 秒キャッシュする
change_paths:
  - cloud/paper-trader/src/

ship:
  pr: null
  merged_sha: null
  deploy_version: null
  deployed_at: null

measurement:
  value: null
  n: null
  window: {start: null, end: null}   # start はデプロイ日の翌日
  deploy_version: null
  query_id: null
  snapshot: null          # measurements/PH-NNNN.json への相対パス

verdict: null             # {decision, reason, decided_at}
learning: null            # 次の仮説を変える 1 文。結果の言い換えは learning ではない
next: null                # 後継 record の id
```

### 不変条件

repo のテストがこれを検査する。1 つでも破れたら `repo_check` が落ちる。

1. `state` は 8 つのいずれか。terminal（`validated` `invalidated` `inconclusive` `abandoned`）に
   なった後の diff は禁止。
2. `claims` は `product` 固定。`not_evidence_for` は非空。
3. `falsification` と `success` は `state` が `drafted` を出た後は編集禁止。
4. `metric` は `metrics.yaml` に存在する key。
5. `falsification` と `success` に `sharpe` `pnl` `dsr` `drawdown` `return` が現れたら落とす。
   product record は市場のことを主張しない。
6. `state` が `measuring` 以降なら `baseline.query_id` と `measurement.query_id` が一致し、
   `baseline.deploy_version` と `measurement.deploy_version` が異なる。
7. `state` が `building` 以降なら `power.decidable` は true。
8. `change_paths` は `loop.yaml` の `allowed_paths` の部分集合。
9. `baseline.window.end` は `ship.deployed_at` の日付より前。`measurement.window.start` は後。
10. 同じ `metric` を持つ `state: measuring` の record は 1 本まで。
11. `state: measuring` の record 総数は `cap_concurrent_measuring` 以下。
12. 直近 7 日の `created_at` を持つ record は `cap_new_hypotheses_per_7d` 以下。
13. product record は `EXP-` `BOOK-` `HYP-` `IDEA-` `F-` を参照しない。他の record 体系と
    ID namespace を共有しない。

## 2. `<records_dir>/loop.yaml`

人間だけが書く。`allowed_paths` に自分自身と `metrics.yaml` を含めない。

```yaml
approved_by: toshipon
approved_at: 2026-09-28

loop_id: quant-lab-product
records_dir: product
id_prefix: PH
branch_prefix: autoloop/product-
metrics_file: product/metrics.yaml

# allowlist。ここに無い path に触れた PR は merge されない
allowed_paths:
  - product/hypotheses/
  - product/measurements/
  - product/journal.md
  - cloud/paper-trader/src/

repo_check: make check
setup_command: uv sync -q --all-extras && cd cloud/paper-trader && npm ci --silent

surface_url: https://quant-paper-trader.build-geeks.workers.dev
surface_paths:
  - cloud/paper-trader/
surface_checks:
  - cd cloud/paper-trader && npx tsc --noEmit
  - cd cloud/paper-trader && npm test

deploy_kind: auto
deploy_verify: npx wrangler deployments status --json
deploy_verify_cwd: cloud/paper-trader
deploy_version_jq: .versions[0].version_id
deploy_wait_seconds: 300
deploy_fallback:
# The surface this loop owns, not another system's monitor. See the note below.
deploy_health: scripts/product/health.sh
avoid_minutes: 0-14

metrics_db: quant-paper-trading-db
metrics_db_cwd: cloud/paper-trader
metrics_table: product_metrics

shared_lock: /Users/toshipon/Library/Application Support/quant-autoloop/cycle.lock

# tick ごとの digest を Slack に流す。省くと macOS の通知とログだけになる。
# 値は 1Password の op:// 参照で、service account 経由で読む。webhook をファイルに書かない。
slack_webhook_op: op://claude-code-dev/quant-autoloop-slack-webhook/password

cap_concurrent_measuring: 1
cap_new_hypotheses_per_7d: 3
cap_merges_per_day: 3
cap_tick_budget_usd: 8
cap_min_sample: 200

# tick に許可する MCP tool（repo の CLAUDE.md が定める外部の記録面を使うときだけ）。
# 名前は完全一致で、wildcard は書かない
mcp_tools:
  - mcp__<server>__<tool>
```

`avoid_minutes` は他の worker の cron が動く分を外すためにある。マージがその worker の再デプロイを
起こす repo では、約定や集計の最中にプロセスが入れ替わらないようにする。

`shared_lock` を書くと、その lock ディレクトリを取れた時だけ tick が走る。同じ repo で別のループが
動いている場合は必ず共有する。2 つのループが同時に main にマージすると、どちらの checks も相手の
変更を見ていない。

`deploy_fallback` を空にすると、自動デプロイが動かなかった時に「マージしたがデプロイされていない」
として exit 1 する。ループが自分で `wrangler deploy` を打つのは、その repo でそれが正解だと人間が
判断した時だけである。

`deploy_health` は **このループが触る surface** の健康を見るコマンドにする。別のシステムの監視を
指してはいけない。理由は 2 つある。1 つは、それがループ自身の変更で壊れうる surface について何も
言わないこと。もう 1 つは、その監視が入っていない機械でループが一度もマージできなくなることで、
安全側に倒れているように見えて実際には動かない gate になる。

何を見るかは surface が決める。cron が定期的に書くなら、その書き込みが止まったことが最も強い信号
である。沈黙は検知しやすく、しかも見逃されやすい。quant-lab では paper trader が 6 日間 1 行も
書かずに誰も気づかなかった。`deploy_health` はその沈黙を見る。

このコマンドを `allowed_paths` に入れない。自分の健康診断を止められるループは、壊れたことに
気づけない。

### 計測の backend

`metrics_backend` が、tick に渡す計測スクリプトを決める。書かなければ `d1` である。

| `metrics_backend` | 行の出どころ | 必要なキー |
|---|---|---|
| `d1`（既定） | Cloudflare D1 のテーブル | `metrics_db`、`metrics_db_cwd`、`metrics_table` |
| `command` | repo が持つスクリプト | `metrics_command` |

`command` は、rollup が D1 に無い repo のためにある。plugin は repo のデータストアを知らない。repo が
1 metric の日次の行を返すスクリプトを持ち、plugin はそれを呼んで集約する。

```yaml
metrics_backend: command
metrics_command: scripts/product/metric-rows.sh
```

スクリプトの契約はこれだけである。

```
<metrics_command> <metric> <from YYYY-MM-DD> <to YYYY-MM-DD>
  -> [{"day": "2026-10-01", "value": 10, "sample": 100}, ...]   # stdout、JSON 配列、1 日 1 行
```

- 行が無い窓は空配列を返す。読めなかったときは非 0 で終わる。空配列と失敗を混ぜない。0 件は本物の
  baseline として採点される
- `value` と `sample` の意味は D1 版と同じで、集約の規則も同じである（key の prefix が決める）
- 鍵が要るなら、スクリプトが自分で読む。plugin は渡さない
- スクリプトは repo のルートを cwd にして実行される。ほかのファイルを source しない 1 本にする

repo のコードが採点の一部になるので、plugin が 3 つを強制する。

1. **実行するのは origin/main の版である。** tick は worktree を書き換えられる。worktree の版を実行すると、
   自分の採点スクリプトを差し替えられる。`metrics_command` の値自体も origin/main の `loop.yaml` から読む
2. **`metrics_command` は `allowed_paths` の外になければならない。** 中にあれば reader は読むのを拒否する。
   今日の origin/main が正しくても、明日のマージで書き換えられるからである
3. **`query_id` はスクリプトの sha も含む。** `m:<reader の sha7>.<スクリプトの sha7>` の形で、行の取り方を
   変えると、集約を変えたときと同じく進行中の record の判定が止まる

backend ごとに reader のファイルが分かれているのは、`query_id` が reader 自身の sha だからである。
1 本にまとめて分岐を足すと、片方を直しただけで、もう片方で進行中の record が全部 mismatch になる。
`loop-run.sh` は選ばれた方を tick の `loop-metrics.sh` として置くので、tick から見た経路は 1 つのまま
である。未知の `metrics_backend` は tick を拒否する。黙って `d1` に倒すと、綴りを間違えた repo が
「読めない」を返し続ける。

### 記録の置き場所（任意）

書かなければ record と journal は git にあり、ここまでの説明どおりに動く。`records_pull` と
`records_push` を書くと、record は外部の記録面にあり、`<records_dir>` はその working copy になる。

```yaml
records_pull: scripts/product/records-pull.sh
records_push: scripts/product/records-push.sh   # 同じスクリプトを両方に書いてもよい（第 1 引数で分ける）
```

2 つは対で書く。片方だけなら tick を拒否する。pull だけなら tick の編集が失われ、push だけなら
読んでいない記録面に上書きする。

スクリプトは `metrics_command` と同じ規則で扱う。

1. キーは origin/main の `loop.yaml` から読み、実行するのは origin/main の版である。runner は tick の
   前に両方を worktree の外へ取り出し、どちらかが取り出せなければ tick を始めない
2. `allowed_paths` の中にあれば拒否する。自分の記録を保存するスクリプトを書き換えられるループは、
   自分の採点表を書ける
3. repo のルート（tick では loop の worktree）を cwd にして `/bin/bash` で実行する。鍵が要るなら
   スクリプトが自分で読む。plugin は渡さない

契約はこれだけである。

```
<records_pull> pull <repo> <records-dir>
<records_push> push <repo> <records-dir> <pulled-copy-dir>
```

| 引数 | 意味 |
|---|---|
| `<repo>` | 絶対パス。tick では loop の worktree、`loop-status.sh` では渡された repo |
| `<records-dir>` | 絶対パス。存在するディレクトリ。tick では `<repo>/<records_dir>`、`loop-status.sh` では一時ディレクトリ |
| `<pulled-copy-dir>` | 絶対パス。pull が書いた直後の `<records-dir>` の写し。push はこれと比べて変わったものを知る |

**pull** は `<records-dir>` に、git にあった時と同じ形のファイルを書く。

- `hypotheses/PH-NNNN.yaml` を全件（この schema の 1 節の形）
- `measurements/PH-NNNN.json`（判定済みの record の証拠）
- `journal.md` に直近のエントリ。tick は末尾 10 エントリを読むので、それ以上あればよい
- 記録面から消えた record のファイルを残さない。`<records-dir>` の record は pull が持ち主である
- `loop.yaml`、`metrics.yaml`、`paused.flag` には触れない。これらは git にある

**push** は `<records-dir>` と `<pulled-copy-dir>` を比べ、変わった record と新しい journal エントリを
記録面に書く。journal のエントリは step 6 の書式のまま末尾に追記されている。

**どちらのスクリプトも symlink を辿らず、通常ファイルだけを読み書きする。** `<records-dir>` は tick が
編集できる場所で、push はその中身を tick の外から読んで外部へ送る。record を秘密ファイルへの symlink や
hard link に差し替えれば、その中身が記録面に持ち出される。pull が symlink を辿って書けば、指す先の
ファイルを上書きする。push はファイルを開く前に通常ファイルであることを確かめ（`[ -f ] && [ ! -L ]`、
または `O_NOFOLLOW`）、pull は既存のパスに書き込まず置き換える。

runner も同じ検査を持つ。`<records-dir>` の下にディレクトリでも、link 数 1 の通常ファイルでもないもの
（symlink、hard link、FIFO など）があれば、次のように止まる。

- tick の後なら push を呼ばずに失敗として扱う（下の表の push 失敗と同じ通知と退避）。digest も
  working copy の journal を読まない。digest は Slack に出るからである
- tick の前なら pull を呼ばずに tick を走らせない。前の tick が残したものなので、人間が取り除くまで
  止まり続ける

**exit code** はどちらも 0 が成功、それ以外は失敗である。

| | 失敗したとき |
|---|---|
| pull | tick を走らせない。runner はログに 1 行書き、通知して非 0 で終わる。push も走らない |
| push | runner はログと通知に「records NOT saved」と書き、digest の先頭にも出す。通常ファイル以外を見つけて push を呼ばなかったときも同じ扱いにする。working copy は `$STATE_DIR/records-unsaved-<stamp>/` に写して残す。次の pull が working copy を上書きするからである。tick の rc は agent の rc のまま |

push は agent が非 0 で終わった tick の後にも走る。途中で死んだ tick も record を動かしているかも
しれないからである。拒否された tick、lock で skip した tick、`paused.flag` で止まった tick では
走らない。

repo の側で必要なことが 4 つある。

1. **pull が書くファイルを `.gitignore` に入れる。** runner は pull の後に
   `git status --porcelain -- <records_dir>` を見て、何か出れば tick を走らせない。git に見える
   record は次の commit に乗り、記録面が検証していない record を merge gate が通すことになる
2. **`allowed_paths` から record の path を外す。** BUILD の PR はコードだけを持つ。record を含む PR は
   gate が拒否するので、誤って `git add -f` した record も main に入らない。`paused.flag` を GUARD に
   置かせるなら `<records_dir>/paused.flag` だけを残す
3. **record の不変条件を push が検査する。** git の diff はもう record を見ていない。1 節の不変条件は
   すべて push の仕事になり、特に diff でしか検査できなかった次の 2 つは push が pulled copy と
   比べて検査する
   - terminal（`validated` `invalidated` `inconclusive` `abandoned`）の record を変えていない（1）
   - `drafted` を出た record の `falsification` と `success` を変えていない（3）

   残り（2、4〜13: `claims` と `not_evidence_for`、metric が `metrics.yaml` にあること、性能語の
   禁止、`query_id` の一致と `deploy_version` の相違、`building` 以降の `power.decidable`、
   `change_paths` ⊂ `allowed_paths`、窓とデプロイ日の前後、同じ metric の `measuring` は 1 本、
   `cap_concurrent_measuring`、`cap_new_hypotheses_per_7d`、ID namespace）も保存の前に検査する。
   1 つでも破れたら何も書かずに非 0 で終わる。一部だけ保存すると、記録面と journal が食い違う
4. **record のテストは record が無くても通る形にする。** `repo_check` は CI でも gate でも走る。CI には
   record が無く、gate の worktree には working copy がある。検査の本体は push にあり、テストは
   無ければ skip する

`loop-merge.sh` は record を読まないので変わらない。`loop-status.sh` は records hooks があると
origin/main の `records_pull` で一時ディレクトリに pull し、そこから盤面を作る。

## 3. `<records_dir>/metrics.yaml`

人間だけが書く。`allowed_paths` に入れない。ループはここにある metric しか使えない。

```yaml
metrics:
  # ドメインの仕事を数える。分母はシステムが作るので人間が見ていなくても溜まる。
  "count:trades_closed":
    kind: operational
    expected_n_per_day: null
    direction: higher_is_better
    description: 1 日に決済した件数。サイクルが仕事をしたかを言う
  "rate:stale_positions":
    kind: operational
    expected_n_per_day: null
    direction: lower_is_better
    description: 建てたまま n 日動いていない建玉の割合。value が分子、sample が全建玉
  "share:top_symbol_exposure":
    kind: operational
    expected_n_per_day: null
    direction: lower_is_better
    description: 最大の 1 銘柄が notional に占める割合

  # surface の metric。分母が人間なので、使う人が少ないと判定に届かない
  "avg_ms:/api/strategies":
    kind: operational
    expected_n_per_day: 400
    direction: lower_is_better
    description: /api/strategies の平均応答時間。human 判定のリクエストのみ
  "errors:/api/run":
    kind: operational
    expected_n_per_day: 24
    direction: lower_is_better
  bot_share:
    kind: operational
    expected_n_per_day: 500
    direction: lower_is_better
```

集約は key の prefix が決める。これは `loop-metrics.sh` の中にあり、ループは変更できない。

| prefix | 集約 | 使いどころ |
|---|---|---|
| `count:` `hits:` | `SUM(value)` | 決済件数、サイクル実行回数、リクエスト数 |
| `rate:` `share:` `errors:` | `SUM(value) / SUM(sample)` | 滞留率、エラー率、1 銘柄への偏り |
| それ以外（`avg_ms:` など） | `SUM(value*sample) / SUM(sample)` | 所要時間、平均滞留日数 |

reader は `n`（窓全体の `SUM(sample)`）と `n_day_max`（1 日あたりの最大）の両方を返す。flow の
仮説は `n` を、snapshot の仮説は `n_day_max` を power の入力にする。snapshot は同じ母集団を毎日
数え直しているので、`n` は日数の分だけ水増しされている。

`count:` と `rate:` がドメイン側の名前で、`hits:` と `errors:` は HTTP の rollup が先に使っていた
同じ算術の別名である。新しい metric はドメイン側の名前を使う。

書く側は必ず value と sample を対で入れる。`rate:` なら value が分子、sample が分母である。
ここがずれると比率が静かに間違う。

`kind: behavioural` の metric は、ユーザーが 1 人の surface では判定に届かない。`power` が false
になり、仮説は abandoned になる。それが正しい振る舞いである。behavioural を測りたいなら、まず
ユーザーを増やす。

`expected_n_per_day` は推測で書かない。`loop-metrics.sh metric <key> <from> <to>` を実際に走らせて
得た値から書く。この数字が power 計算の入力であり、間違っていると劇場になる。

## 4. `<records_dir>/journal.md`

1 tick 1 エントリ。書式は [`cycle-prompt.md`](cycle-prompt.md) の step 6 にある。

## 稼働状況（任意）

```yaml
status_db: quant-loop-db          # D1 の名前。無ければ機能ごと no-op
status_db_cwd: cloud/paper-trader  # npx wrangler を走らせる場所（repo 相対）
```

テーブルは 1 つだけである。

```sql
CREATE TABLE loop_ticks (
  loop_id    TEXT    NOT NULL,
  stamp      TEXT    NOT NULL,   -- 20260930-0030（UTC）
  host       TEXT    NOT NULL,   -- scutil --get ComputerName
  started_at TEXT    NOT NULL,
  ended_at   TEXT,
  rc         INTEGER,
  cost_usd   REAL,
  action     TEXT,               -- journal の「行動:」から取る。動かさなければ NULL
  branch     TEXT,
  pr         INTEGER,
  moved      INTEGER NOT NULL DEFAULT 0,
  PRIMARY KEY (loop_id, stamp, host)
);
```

ここに verdict・metric・仮説を入れてはいけない。判断の置き場所は git だけである。`moved` と
`action` は journal の diff から導出する。エージェントの最終メッセージからは取らない。ship したと
言って何も残さなかった tick は `moved=0` で閉じる。
