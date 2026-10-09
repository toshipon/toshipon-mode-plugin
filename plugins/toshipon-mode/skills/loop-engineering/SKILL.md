---
name: loop-engineering
description: 仮説検証のループを自律で回す。1 tick で 1 判断だけ進め、実装・テスト・マージ・デプロイ・計測・判定・仮説へのフィードバックを閉じる。BUILD できる draft が無いとき（尽きたとき、または計測中の metric と重なって待っているとき）はペルソナを足してインタビューし新しい仮説を作る。Use when setting up or running an autonomous hypothesis-validation loop for a product, scheduling recurring claude -p cycles, or asking what the next loop tick will do.
---

# loop engineering

プロダクトを仮説検証で成長させるループを、人間の承認を待たずに回す。1 回の `claude -p` 実行が
1 tick であり、1 tick は 1 つの判断しか進めない。

tick は純関数である。入力はコミット済みの record 群（records hooks を使う repo では、runner が
外部の記録面から書き出した record 群）と、計測スクリプトが返す数字だけ。前の tick の
記憶は持たず、state はすべてファイルに書かれている。途中で死んだ tick の後始末も、次の tick が
state から判断する。

```
drafted → building → shipped → measuring → validated / invalidated / inconclusive
   ↑                                                      │
   └──── interview（BUILD できる drafted が無い時）←────┘
```

## このループが壊れる 3 つの形

設計はこの 3 つを止めるためにある。読まずに使うと 3 つとも踏む。

### 1. 劇場になる（最も起きやすい）

ユーザーが 1 人しかいないプロダクトで behavioural metric を測ると、窓が終わってもサンプルが足りず、
判定は永久に `inconclusive` になる。「仮説を立て、作り、デプロイし、何も分からない」を繰り返す
装置ができる。動いているように見えるので気づきにくい。

止め方は 2 つある。1 つは metric を **operational** に寄せること。もう 1 つは `power` ブロックを
drafted の必須項目にすること。`decidable: false` の仮説は building に進めず abandoned にする。
判定できるかどうかは設計を書いた日に分かる。データが来てから気づくのでは遅い。

### operational は「HTTP の遅延」という意味ではない

ここを取り違えると、劇場を避けたつもりで別の劇場に入る。

operational な metric とは、**そのシステムが自分の仕事をしたかどうか**を言う数字である。画面の
応答時間やエラー率はその一例にすぎず、しかも誰も開かない画面では分母が 0 になる。分母はドメイン
が作る。そのシステムが繰り返し行う仕事を数えれば、人間が見ていなくてもサンプルは溜まる。

取引システムなら、こうなる。

| 測る | 測らない |
|---|---|
| 1 サイクルあたり何件決済したか | その決済が儲かったか |
| 何日動かしていない建玉があるか | その建玉の含み損益 |
| シグナルのうち何件が注文になったか | シグナルの的中率 |
| エクスポージャが 1 銘柄にどれだけ寄っているか | どの銘柄が上がるか |
| 日次サイクルが最後まで走った割合 | 戦略の Sharpe |

左は「動かすべきものが動いたか」で、右は「市場が報いたか」である。左は仕組みの不具合を指すので
直せる。右は edge の主張なので、多重性の会計に入り、このループの守備範囲ではない（後述）。

**実例。** ある paper trading の worker で、UI の metric を 7 時間測ったらイベントは 8 件で全部
cron、人間のリクエストは 0 件だった。一方その間、同じ D1 には 250 件の建玉が 7 日間放置され、
決済は通算 4 件しかなく、含み損の 99% が 1 銘柄に寄っていた。測るべきものはずっとそこにあり、
HTTP の側を見ていたから見えなかった。

### 分母の探し方

surface の種類ごとに、繰り返される仕事が分母になる。

- 取引・バッチ・パイプライン → 1 サイクルあたりの処理件数、滞留件数、完走率
- API・worker → cron の実行、キューの消化、リトライ
- コンテンツ・ツール → 生成物の件数、失敗率、再実行率

**そのシステムのドメインのテーブルを先に見る。** HTTP のイベントより、ドメインの行の方がたいてい
多く、意味がある。

### flow と snapshot を混同しない

metric には 2 種類ある。ここを混ぜると power gate が水増しされたサンプルで通る。

- **flow**（その日に起きた件数。決済数、エラー数、リクエスト数）。日をまたいで足せる。7 日なら
  1 日分の 7 倍のサンプルが本当に集まる
- **snapshot**（その時点の状態。滞留している建玉、キューの長さ、建玉を持つ戦略の割合）。**同じ
  母集団を毎日数え直しているだけ**なので、足しても増えない

`loop-metrics.sh` の `n` は窓全体の `SUM(sample)` である。snapshot でこれを power の入力にすると、
日数の分だけサンプルを水増しして数える。1 日あたりの最大を返す `n_day_max` も同時に返すので、
snapshot の仮説はそちらを使う。

`metrics.yaml` の description に flow か snapshot かを書く。書いていない metric で power を計算
しない。

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

**health check は自分が触る surface を見る。** 別のシステムの監視を指した gate は、ループ自身の
変更で壊れうる画面について何も言わず、しかもその監視が入っていない機械ではマージを常に拒否する。
安全側に倒れているように見えて、実際には動かない gate である。cron が定期的に書く surface なら、
書き込みが止まったことが最も強い信号になる。沈黙は検知しやすく、しかも見逃されやすい。

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
| 4 | `measuring` の数 < `cap_concurrent_measuring` かつ、`measuring` 中の metric と重ならない `drafted` がある | **BUILD** |
| 5 | 行 4 に当たらない（いま BUILD できる `drafted` が無い）かつ、直近 7 日の新規仮説が `cap_new_hypotheses_per_7d` 未満 | **INTERVIEW** |
| 6 | 上のどれでもない | **WAIT** |

行 5 は「`drafted` が 1 件もない」ではない。`drafted` があっても、どれも `measuring` 中の metric と
重なっていれば、その窓が閉じるまで（`power.horizon_days`、たいてい 2 週間）BUILD できない。旧い行 5
はその間 INTERVIEW も止め、ループは何週間も WAIT だけを返した（ある repo の product loop、
2026-10-08。PH-0001 が measuring、同じ metric の PH-0002 が drafted のまま）。インタビューは計測に
触れないので、窓を待つ間に回しても交絡しない。作りすぎは `cap_new_hypotheses_per_7d` が止める。

WAIT が正しい結果であることを忘れない。窓が閉じるのを待つ間に同じ metric を動かす変更を出すと、
2 つの効果が混ざって両方の仮説が死ぬ。`cap_concurrent_measuring` はスループットの目標ではなく、
交絡を防ぐ予算である。同じ metric を見る `measuring` は常に 1 本までにする。

### 何も動かなかった tick は ship しない

**ship するのは、record の state が動いたか、コードを変えたときだけである。** どちらも無い tick は
journal を書かず、PR も出さない。tick が走ったことは runner のログと通知に残る。

これは頻度を上げると効いてくる。3 時間ごとに回せば 1 日 8 tick になり、そのうち動きがあるのは
数回である。残りを毎回 PR にすると、`cap_merges_per_day` を使い切り、`needs-human` の PR が
積み上がり、マージのたびに関係ない worker まで再デプロイされる。

journal はループが**何をしたか**の記録であって、動いていることの証明ではない。動いている証明は
runner のログが持つ。

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
   `deploy_verify` の現在値で、runner が tick の開始時に読んでプロンプトに渡す（tick 自身は
   `deploy_verify` のコマンドを禁じられている）。渡された値が `unknown` なら baseline を取らない。デプロイ後に取った baseline は baseline ではない。
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
6. **ship する。** commit、`loop-push.sh`、`gh pr create`、`loop-merge.sh <pr>`。record は同じ PR で
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
7. `invalidated` も `inconclusive` も資産である。record を消さない。後継があれば `next` に書く。

### INTERVIEW

いま BUILD できる `drafted` が無い時に走る（決定表の行 5）。`drafted` が尽きた時だけでなく、
残っている `drafted` がすべて `measuring` 中の metric と重なって待っている時も走る。ここで作るのは
仮説の候補であって証拠ではない。

`measuring` の数が `cap_concurrent_measuring` に届いていなければ、別の metric の仮説を優先する。
それならいますぐ BUILD に進める。cap が埋まっているなら、どの metric でも次の窓で順に進むので、
同じ metric の仮説を作ってよい。そのときは `priority` で順番を付け、互いの効果が混ざらないよう
1 本ずつ measuring に入る前提で `change_summary` を書く。

1. 既存の全 record と journal を読む。同じ問いを二度立てない。
2. 既存ペルソナを見る。埋まっていない面を 1 つ選び、1 体足す。職業、goals、frustrations、
   behaviors を埋める。ペルソナとインタビューの置き場所は repo の CLAUDE.md が決める（下の
   「外部の記録面」）。
3. **実際の surface を観測する。** 対話なら `claude-in-chrome` か `superset:browser` で操作する。
   headless なら `surface_url` に WebFetch する。認証の裏にあって届かないなら、リポジトリの
   dev サーバを起動して同じ route を叩く。画面を一度も見ずに書いたインタビューは作文であり、
   観測できなかったならそう書く。観測したことを材料に擬似インタビューを行い、
   記録面に登録する。records には写しを残さない。journal の `Did` に persona と interview の id を
   書き、`source: synthetic` を本文に明記する。記録面が無いか登録に失敗したら、本文を journal に
   残して先に進む。

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

## 外部の記録面

この skill は特定の外部サービスを知らない。repo が record の外にも書く場所（課題管理、
仮説管理のツールなど）を使うなら、何をいつどこへ書くかは repo の `CLAUDE.md` が定める。tick は
`CLAUDE.md` を読むので、そこに書けば BUILD・EVALUATE・INTERVIEW の各段で従う。

- 使う tool は `loop.yaml` の `mcp_tools` で許可する。許可していない tool は tick から呼べない
- 外部への書き込みが失敗しても record の state を止めない。journal に書いて先に進む
- 外部の記録面を読んで判定を動かすかは、repo の `CLAUDE.md` の定めによる。定めが無ければ、
  state も verdict も record が決める

## 記録を外部の記録面に置く（records hooks）

既定では record と journal は git にあり、record が動いた tick は PR を出してマージする。record の
更新のたびに PR とマージが要り、`cap_merges_per_day` を record だけで使い切ることがある。

`loop.yaml` に `records_pull` と `records_push` を書くと、record は外部の記録面に置かれる。runner は
tick の前に pull で `<records_dir>` に書き出し、tick の後に push で保存する。tick から見たファイルの
形は変わらない。変わるのは次の 4 点である。

- `<records_dir>` の record は git が無視する working copy になる。tick は record も journal も
  commit しない（runner がプロンプトに `references/cycle-prompt-records-store.md` を足して伝える）
- record だけが動いた tick は commit も PR も出さない。BUILD の PR はコードだけを持つ
- 通知の journal は、git の diff ではなく pull した写しとの差分から取る
- record の不変条件は repo の push スクリプトが検査する。git の diff が record を見なくなるので、
  ここを repo が引き受けないと、terminal の record の書き換えも基準の事後変更も通る

pull が失敗したら tick は走らない。push が失敗したら digest に「records NOT saved」と出し、
working copy を退避する。working copy に symlink や hard link があれば、秘密ファイルの持ち出しを
防ぐため runner は push を呼ばない。2 つのスクリプトは `metrics_command` と同じく origin/main の版を
`allowed_paths` の外から実行する。契約（引数、exit code、pull が書くもの、push が読むもの）と repo 側で
必要な設定は [`references/record-schema.md`](references/record-schema.md) の「記録の置き場所」にある。

キーを書かなければ、挙動は records hooks が無かった時と同じである。

## 境界

`<records_dir>/loop.yaml` が `allowed_paths` を持つ。これは allowlist であり denylist ではない。
ループが触れてよい path を列挙し、それ以外に触れた PR は `loop-merge.sh` がマージを拒否する。

denylist を使わない理由は、書き漏らした path が通ってしまうことである。allowlist は書き漏らしを
「マージされない」で止める。

### 性能の主張はこのループの仕事ではない

ドメインの metric を測り始めると、その先に「儲かったか」「効いたか」を測りたくなる。そこが境界
である。

このループが言えるのは「仕組みが自分の仕事をしたか」までである。その先の「だから成果が出た」は
別の主張であり、別の会計に属する。取引なら多重性の補正、広告なら実験計画、推薦なら off-policy
評価というように、その分野が持つ厳密な枠組みがある。それを通さない性能の主張は、手続きだけ
正しく見えて中身が空になる。

だから record に `claims` と `not_evidence_for` を書かせ、テストで強制する。`falsification` と
`success` に性能を表す語が現れたら落とす。ループを止めるためではなく、**2 つの主張を混ぜない**
ためである。

性能を測りたいなら、それ専用のループか、その分野の枠組みを持つ既存の仕組みに渡す。このループは
その土台を整える側にいる。土台が壊れていると性能の数字そのものが読めなくなるので、無関係では
ない。

### 稼働状況は記録ではなく計測器として持つ

WAIT で終わった tick は commit も push もしない。だから「ループが生きているか」を repo から
答えられない。動いていない端末と、待っている端末が同じ見え方になる。

`status_db` を設定すると、runner が tick ごとに 1 行書く。どの端末がいつ回し、rc と費用と行動が
何だったか。**判定・指標・仮説は record の置き場所（git、または records hooks の記録面）だけに置く。**
不変条件の検査が届かない場所に判断が置かれると、答えが 2 つある状態になる。

書くのは runner であってエージェントではない。tick は secret も `curl` も `wrangler` も持たない。
その性質が「自分の採点表を書かせない」を成立させているので、telemetry のために崩さない。書き込みの
失敗は握り潰す。計測器が tick の成否を決めてはいけない。

`status_db` が無い repo では全部 no-op になる。

### sandbox の中身は付け替えてよい

surface が sandbox（本番の判断に自動では届かない場所）なら、その中身の入れ替えはこのループの
仕事にしてよい。何を動かすか、何を止めるかは運用の一部である。

境界は 2 つで引く。**1 つは allowlist。** sandbox の外へ出る path を入れなければ、ループは
物理的に本番に届かない。言葉ではなくパスで止める。**もう 1 つは語彙。** 入れ替えの理由は
「動かすべきものが動いたか」で書く。決済できない、エラーを出す、取り残しが出る。「儲からない
から外す」は性能の主張であり、別の会計に属する。

sandbox の結果を後から「効いたから本番に上げる」と引用すると、どこにも数えられていない試行に
なる。だから record の `not_evidence_for` を維持し、昇格は人間か、その会計を持つ仕組みに残す。

### allowed_paths に入れてはいけないもの

- `loop.yaml` 自身。ループが自分の境界を広げられないことが唯一の硬い不変条件である
- `metrics.yaml`。自分の採点表を書けるループは検証をしていない
- record の不変条件を検査するテスト

## Scripts

`loop-run.sh` は gate 群を `$STATE_DIR/bin` に複製してから agent を起動する。agent の worktree に
ある編集済みの gate が使われることはない。gate は plugin 側にあるので、対象リポジトリの PR では
そもそも変えられない。

| script | 役目 |
|---|---|
| `scripts/loop-run.sh <repo> [label]` | 1 tick を headless で回す。records hooks があれば tick の前に pull、後に push する |
| `scripts/loop-status.sh <repo>` | state 別の件数、劇場チェック、record 一覧 |
| `scripts/loop-check.sh` | repo_check と surface_checks。agent の唯一の check 経路 |
| `scripts/loop-metrics.sh` | 計測の唯一の経路。SQL はここにあり agent は識別子しか渡さない |
| `scripts/loop-metrics-command.sh` | 同じ契約で、行を repo のスクリプト（`metrics_command`）から読む版。`metrics_backend: command` の repo では、runner がこれを tick の `loop-metrics.sh` として置く |
| `scripts/loop-push.sh` | `branch_prefix` に一致するブランチだけを push する |
| `scripts/loop-merge.sh <pr>` | allowlist、cap、health、merge window、checks、マージ、デプロイ確認 |
| `scripts/loop-install.sh <repo> [hour] [min]` | launchd に仕込む。`hour` は `15`（日次）か `*/3`（3 時間ごと）。`--uninstall <loop_id>` |
| `scripts/loop-selftest.sh` | config の読み取りと allowlist 照合と state 集計、records hooks を使う runner の順序を fixture で検査する |

別の端末で動かす手順は [`references/another-machine.md`](references/another-machine.md) にある。
**ループを動かす端末は常に 1 台だけにする。** 2 台が同じ main にマージすると、どちらの checks も
相手の変更を見ていない。移行するなら先に移行元で `loop-install.sh --uninstall` を実行する。

tick ごとの digest は `slack_webhook_op` を設定すると Slack に流れる。設定しなければ macOS の
通知とログだけになる。webhook は 1Password の op:// 参照で読み、ファイルには書かない。

launchd の job は、登録した時点の plugin ディレクトリを指す。そのパスにはバージョンが入っている。
plugin を更新すると新しいバージョンのディレクトリが増えるだけで古い方は残るので、**launchd は
古いスクリプトを黙って走らせ続ける**。plugin を更新したら `loop-install.sh` を再実行する。

`loop-selftest.sh` は plugin を変更したら毎回 `/bin/bash` で走らせる。allowlist の照合はこの設計で
最も効く 6 行なので、`loop_path_allowed` として 1 箇所に置き、そこを直接叩いている。gate と repo 側の
record テストは同じ規則を見るが、問う相手が違う。gate は「この PR をマージしてよいか」を決め、repo の
テストは「この record の形が正しいか」を決める。

`loop-merge.sh` の exit code は 0 = マージしてデプロイ確認、1 = checks かデプロイの失敗、
3 = 人間が見る必要がある、4 = merge window の外なので後で再試行。

頻度を上げるときは `cap_merges_per_day` を見直す。3 時間ごと（1 日 8 tick）でも、ship するのは
record が動いた tick だけなので毎回マージにはならない。それでも動きの多い日は上限に当たる。
当たった tick は PR を `needs-human` で残して終わるので、放置すると溜まる。

repo が用意するのは `loop.yaml` と `metrics.yaml`、そして record の不変条件を検査するテスト 1 本
である。テストは repo 自身の toolchain で書く。schema はこの skill が定義し、強制は repo が行う。

## Journal

tick が終わるたび `<records_dir>/journal.md` に 1 エントリ追記する。何もしなかった tick も書く。
書式は [`references/cycle-prompt.md`](references/cycle-prompt.md) の step 6 が持つ。通知に出る
5 bullet と、後で読む記録の 2 段構えである。
