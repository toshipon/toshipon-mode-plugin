---
name: loop-engineering
description: 仮説検証のループを自律で回す。1 tick で 1 判断だけ進め、実装・テスト・マージ・デプロイ・計測・判定・仮説へのフィードバックを閉じる。draft が尽きたらペルソナを足してインタビューし新しい仮説を作る。KaizenLab MCP をトラッキング面に使う。Use when setting up or running an autonomous hypothesis-validation loop for a product, scheduling recurring claude -p cycles, or asking what the next loop tick will do.
---

# loop engineering

プロダクトを仮説検証で成長させるループを、人間の承認を待たずに回す。1 回の `claude -p` 実行が
1 tick であり、1 tick は 1 つの判断しか進めない。

tick は純関数である。入力はコミット済みの record 群と、計測スクリプトが返す数字だけ。前の tick の
記憶は持たず、state はすべてファイルに書かれている。途中で死んだ tick の後始末も、次の tick が
state から判断する。

```
drafted → building → shipped → measuring → validated / invalidated / inconclusive
   ↑                                                      │
   └──────────── interview（drafted が尽きた時）←──────────┘
```

## このループが壊れる 3 つの形

設計はこの 3 つを止めるためにある。読まずに使うと 3 つとも踏む。

### 1. 劇場になる（最も起きやすい）

ユーザーが 1 人しかいないプロダクトで behavioural metric を測ると、窓が終わってもサンプルが足りず、
判定は永久に `inconclusive` になる。「仮説を立て、作り、デプロイし、何も分からない」を繰り返す
装置ができる。動いているように見えるので気づきにくい。

止め方は 2 つある。metric を **operational** に寄せること。error rate、平均応答時間、cron の成功率、
1 tick のコスト、通知の遅れ。これらは cron とシステムが分母を作るので、人間が 1 人でもサンプルが
機械的に溜まる。もう 1 つは `power` ブロックを drafted の必須項目にすること。`decidable: false` の
仮説は building に進めず abandoned にする。判定できるかどうかは設計を書いた日に分かる。データが
来てから気づくのでは遅い。

**ループ自体の falsification。** terminal に達した record のうち `inconclusive` が 50% を超えたら、
このループは劇場である。`/toshipon-mode:loop-status` がこの比率を出す。超えたら metric 設計を
変えるか、ループを止める。

### 2. 自分の採点表を書く

同じ agent が変更を作り、metric を測り、判定を書く。query を少し変えれば自分の仮説は通る。

止め方は query の凍結である。計測コードと metric 定義は `allowed_paths` の外に置く。ループは
`metrics.yaml` に既にある metric しか使えず、新しい metric は人間の PR で入る。計測の SQL は
`loop-metrics.sh` の中にあり、これは plugin 側にあるのでリポジトリの PR では変えられない。
`query_id` はそのスクリプト自身の sha7 なので、採点方法を変えると進行中の record が全部 mismatch
になり、判定は静かに再採点されるのではなく止まる。

さらに baseline と measurement の `deploy_version` が同一の record は判定しない。変更前を測って
いない baseline は baseline ではない。

### 3. マージがデプロイを起こす

マージが自動デプロイにつながる repo では、このループのマージが**関係ない worker まで再デプロイ
する**。実弾を扱う worker がある repo では、これが最大の危険である。

止め方は層を重ねることである。デプロイ経路を path で絞る（repo 外の設定なのでテストが届かない。
だから唯一の防御にしない）、cron の実行時刻を避ける `avoid_minutes`、マージ前の health check、
`cap_merges_per_day`。どれか 1 つが外れても事故にならない形にする。

## State model

仮説 1 件が 1 ファイルである。`<records_dir>/hypotheses/PH-NNNN.yaml`。全フィールドと不変条件は
[`references/record-schema.md`](references/record-schema.md) にある。

状態は 8 つ。`state` 以外に進行状況を表す boolean を置かない。shipped かどうかは `state` と
`ship.merged_sha` の有無で決まり、専用のフラグを持たない。

| from | to | 条件 | 書く者 |
|---|---|---|---|
| drafted | building | baseline / falsification / power.decidable が揃った | tick |
| drafted | abandoned | power.decidable が false、または不要と判断した | tick（理由必須） |
| building | shipped | PR がマージされ、新しい deploy version を確認した | tick（gate の出力から） |
| building | drafted | PR が閉じた | tick |
| building | abandoned | checks が 2 tick 続けて green にならない | tick |
| shipped | measuring | デプロイ翌日以降に event を観測した | tick |
| shipped | building | health check 異常。revert PR を作る | tick |
| measuring | validated | `success` が真 | tick |
| measuring | invalidated | `falsification` が真 | tick |
| measuring | inconclusive | horizon に達し、どちらも真でない | tick |

terminal は `validated` `invalidated` `inconclusive` `abandoned` の 4 つ。terminal になった record は
編集しない。訂正が必要なら新しい `PH-` を切り、`next` で系譜を残す。事後に基準を動かすのは判定の
捏造である。

`shipped` が `measuring` と別なのは、デプロイできても event が来ないことがあるからである。人間が
1 人の surface では、これが普通に起きる。event を見ずに始めた窓は測っていない窓である。

## Tick の決定表

上から評価し、最初に一致した 1 行だけを実行して止まる。複数を実行しない。

| # | 条件 | 行動 |
|---|---|---|
| 0 | `paused.flag` がある、または health check が異常 | **GUARD** |
| 1 | `building` の record がある | **RECONCILE** |
| 2 | `shipped` の record にデプロイ翌日以降の event がある | **ADVANCE** |
| 3 | `measuring` の record が horizon に達した | **EVALUATE** |
| 4 | `measuring` の数 < `cap_concurrent_measuring` かつ `drafted` がある | **BUILD** |
| 5 | `drafted` が 1 件もない | **INTERVIEW** |
| 6 | 上のどれでもない | **WAIT** |

WAIT が正しい結果であることを忘れない。窓が閉じるのを待つ間に同じ metric を動かす変更を出すと、
2 つの効果が混ざって両方の仮説が死ぬ。`cap_concurrent_measuring` はスループットの目標ではなく、
交絡を防ぐ予算である。同じ metric を見る `measuring` は常に 1 本までにする。

## 各行動

### GUARD

`deploy_health` を実行する。異常があれば、直前の tick が出した変更が原因かを判断する。原因なら
revert PR を作って merge gate に渡し、該当 record を `building` に戻す。原因が分からなければ
`<records_dir>/paused.flag` をコミットし、journal に書いて止まる。人間が消すまで次の tick は
step 0 で止まる。fail closed が既定である。

### RECONCILE

前の tick が途中で死んだ後始末である。`ship.pr` を `gh pr view` で見る。

- merged で新しい deploy version がある → `shipped` にし、`ship` を埋める
- merged だがデプロイが確認できない → journal に書いて止まる。人間に回す
- closed / 存在しない → `drafted` に戻し、`ship` を消す
- open → 何もせず journal に書いて終わる

### ADVANCE

`loop-metrics.sh events <deployed_at の翌日>` で event 件数を見る。1 件以上なら `measuring` にし、
`measurement.window.start` をデプロイ翌日に確定する。0 件なら `shipped` のまま、journal に「まだ
観測されていない」と書いて終わる。これが `power.horizon_days` の 2 倍続いたら `abandoned` にする。
誰も触らない画面の仮説は検証できない。

### BUILD

1 tick で 1 件だけ。`priority` の小さい `drafted` を選ぶ。

1. **baseline を先に取る。** `loop-metrics.sh metric <key> <from> <to>` を実行し、返った JSON を
   record の `baseline` に写す。`window.end` は今日の前日にする。`deploy_version` は
   `deploy_verify` の現在値である。デプロイ後に取った baseline は baseline ではない。
2. **power を計算する。** `metrics.yaml` の `expected_n_per_day` と `power.horizon_days` から、
   `success` の差が検出できるかを判断し、`power.decidable` と根拠を書く。false なら `abandoned`
   にして終わる。ここで止まるのは正しい仕事である。

   **「判定できない」と「まだ分からない」を混同しない。** `expected_n_per_day` が null なのは、
   その metric が判定に届かないからではなく、実測がまだ無いからである。null のときは
   `decidable` を false にせず、record を `drafted` のまま残し、journal に「rollup の実測を待って
   いる」と書いて終わる。まだ分からないものを abandoned にすると、測れば通ったはずの仮説を捨てる。
3. **falsification と success を確定させる。** どちらかが空なら `drafted` に戻して理由を書く。
   この 2 つは以後編集しない。
4. **実装する。** `toshipon-mode` skill の Feature playbook に入る。触れるのは `allowed_paths` の
   中だけ。`metrics.yaml` と `loop.yaml` と計測コードには触れない。
5. **`loop-check.sh` を通す。** これが gate と同じ checks を同じ順で走らせる唯一の経路である。
6. **KaizenLab に verification canvas を作る。** 名前は `PH-NNNN: <title>`。`sections.why` に
   purpose / targetHypothesis / successMetrics、`sections.how` に `method: analytics` と
   mvpDefinition を入れ、`status: in-progress` にする。canvas id を record に書く。
7. **ship する。** commit、`loop-push.sh`、`gh pr create`、`loop-merge.sh <pr>`。record は同じ PR で
   `building` にし、`ship.pr` を書く。exit 3 と exit 4 は `building` のまま残す。

### EVALUATE

1. `loop-metrics.sh metric <key> <デプロイ翌日> <前日>` を実行し、`measurement` を埋める。
2. `query_id` が baseline と一致しない、または `deploy_version` が baseline と同じなら、判定を
   書かず journal に書いて止まる。計測器が動いている。
3. `measurement.n < cap_min_sample` で horizon に達した → `inconclusive`。判定を捏造しない。
4. `falsification` と `success` を機械的に当てる。発火した方を採る。両方偽なら `inconclusive`。
5. `verdict` に `decision` `reason` `decided_at` を書く。`learning` は次の仮説を変える一文にする。
   結果の言い換えは learning ではない。
6. 計測の生の JSON を `<records_dir>/measurements/PH-NNNN.json` にコミットする。これが証拠である。
7. KaizenLab を更新する。canvas の `sections` は read-modify-write で、`what.quantitativeResults` と
   `what.qualitativeResults` と `what.nextAction` を埋めてから `status` を動かす。3 つが空のまま
   status を動かすと canvas が嘘になる。`add_learning` を 1 件、tags に `PH-NNNN` を入れる。
8. `invalidated` も `inconclusive` も資産である。record を消さない。後継があれば `next` に書く。

### INTERVIEW

`drafted` が尽きた時だけ走る。ここで作るのは仮説の候補であって証拠ではない。

1. 既存の全 record と journal を読む。同じ問いを二度立てない。
2. `list_personas` で既存ペルソナを見る。埋まっていない面を 1 つ選び、`create_persona` で 1 体
   足す。`occupation`（`role` ではない）、`goals`、`frustrations`、`behaviors` を埋める。
3. **実際の surface を観測する。** 対話なら `claude-in-chrome` か `superset:browser` で操作する。
   headless なら `surface_url` に WebFetch する。認証の裏にあって届かないなら、リポジトリの
   dev サーバを起動して同じ route を叩く。画面を一度も見ずに書いたインタビューは作文であり、
   観測できなかったならそう書く。観測したことを材料に擬似インタビューを行い、`create_interview`
   で登録し、`<records_dir>/interviews/INT-NNN.md` に同じ内容を残す。`source: synthetic` を明記する。

   **surface が読めなければ、そこで止まる。** route がエラーを返す、画面が描画されない、起動
   できないといった場合、それは仮説の材料ではなく defect である。インタビューには観測した事実
   （叩いた route、status、body、ログ）だけを書き、journal の `要対応` に defect を挙げて終わる。
   壊れた画面についての仮説を書いてはいけない。壊れた surface の上で取った baseline は、その後の
   measurement が何と比較されているのか分からなくなる。

   決定表に surface の health を見る行は無い。deploy_health は別 worker の監視であって、この
   ループが触る画面のことは何も言わない。だから surface が生きているかは、観測しに行った
   INTERVIEW と BUILD がその場で確かめる。
4. `metrics.yaml` に既にある metric で測れる `drafted` を 1 件から 3 件作る。測れない着想は record に
   せず、journal に「人間が metric を足せば測れる候補」として残す。
5. `cap_new_hypotheses_per_7d` を超えない。
6. **擬似インタビューを判定の根拠にしない。** 仮説を生む装置であり、証拠は計測だけから来る。
   この区別を journal に毎回書く。

## KaizenLab との関係

同期は一方向である。ディスクの record が正で、KaizenLab はそこから派生した閲覧面である。

- **書く。** canvas、learning、persona、interview
- **読む。** ペルソナ一覧と、既存 canvas の重複確認まで
- **読んで判定を動かさない。** state も verdict も record が決める

整合性チェックは KaizenLab に届かない。テストが走らない場所の数字は検証済みではない。第二の記録を
根拠にすると、どちらが正か分からなくなる。

## 境界

`<records_dir>/loop.yaml` が `allowed_paths` を持つ。これは allowlist であり denylist ではない。
ループが触れてよい path を列挙し、それ以外に触れた PR は `loop-merge.sh` がマージを拒否する。

denylist を使わない理由は、書き漏らした path が通ってしまうことである。allowlist は書き漏らしを
「マージされない」で止める。

`allowed_paths` に**入れてはいけない**もの。

- `loop.yaml` 自身。ループが自分の境界を広げられないことが唯一の硬い不変条件である
- `metrics.yaml`。自分の採点表を書けるループは検証をしていない
- record の不変条件を検査するテスト

## Scripts

`loop-run.sh` は gate 群を `$STATE_DIR/bin` に複製してから agent を起動する。agent の worktree に
ある編集済みの gate が使われることはない。gate は plugin 側にあるので、対象リポジトリの PR では
そもそも変えられない。

| script | 役目 |
|---|---|
| `scripts/loop-run.sh <repo> [label]` | 1 tick を headless で回す |
| `scripts/loop-status.sh <repo>` | state 別の件数、劇場チェック、record 一覧 |
| `scripts/loop-check.sh` | repo_check と surface_checks。agent の唯一の check 経路 |
| `scripts/loop-metrics.sh` | 計測の唯一の経路。SQL はここにあり agent は識別子しか渡さない |
| `scripts/loop-push.sh` | `branch_prefix` に一致するブランチだけを push する |
| `scripts/loop-merge.sh <pr>` | allowlist、cap、health、merge window、checks、マージ、デプロイ確認 |
| `scripts/loop-install.sh <repo> [h] [m]` | launchd に日次 tick を仕込む。`--uninstall <loop_id>` |
| `scripts/loop-selftest.sh` | config の読み取りと allowlist 照合と state 集計を fixture で検査する |

`loop-selftest.sh` は plugin を変更したら毎回 `/bin/bash` で走らせる。allowlist の照合はこの設計で
最も効く 6 行なので、`loop_path_allowed` として 1 箇所に置き、そこを直接叩いている。gate と repo 側の
record テストは同じ規則を見るが、問う相手が違う。gate は「この PR をマージしてよいか」を決め、repo の
テストは「この record の形が正しいか」を決める。

`loop-merge.sh` の exit code は 0 = マージしてデプロイ確認、1 = checks かデプロイの失敗、
3 = 人間が見る必要がある、4 = merge window の外なので後で再試行。

repo が用意するのは `loop.yaml` と `metrics.yaml`、そして record の不変条件を検査するテスト 1 本
である。テストは repo 自身の toolchain で書く。schema はこの skill が定義し、強制は repo が行う。

## Journal

tick が終わるたび `<records_dir>/journal.md` に 1 エントリ追記する。何もしなかった tick も書く。
書式は [`references/cycle-prompt.md`](references/cycle-prompt.md) の step 6 が持つ。通知に出る
5 bullet と、後で読む記録の 2 段構えである。
