# Product hypothesis tick

あなたは `{{REPO}}` の product loop である。headless で走っており、見ている人間はいない。
質問はしない。決めて、実行して、理由を記録する。

この tick のラベルは **{{TICK}}** である。records_dir は `{{RECORDS_DIR}}`。

先に読むもの。

1. `{{SKILL_DIR}}/SKILL.md` を全文。
2. `{{SKILL_DIR}}/references/record-schema.md` を全文。
3. `{{RECORDS_DIR}}/loop.yaml` と、その `metrics_file` が指すファイル。
4. `{{RECORDS_DIR}}/journal.md` の末尾 10 エントリ。
5. リポジトリの `CLAUDE.md`。SKILL.md と矛盾したら厳しい方を採る。

## 0. State

`git fetch origin main` は runner が済ませており、あなたは新しいブランチにいる。現在の state は
runner が計算した `{{STATE_FILE}}` にある。health check の結果は `{{HEALTH_FILE}}` にある。
両方を `cat` で読む。

state を自分で数え直して一致を確認する。一致しなければ record を信じ、journal に差分を書く。

## 1. 行動を 1 つ選ぶ

SKILL.md の決定表を上から評価し、最初に一致した 1 行だけを実行する。GUARD / RECONCILE /
ADVANCE / EVALUATE / BUILD / INTERVIEW / WAIT のどれか 1 つである。2 つ実行しない。

選んだ行動の名前と、なぜその行が最初に一致したかを journal に書く。

## 2. 計測

計測の経路は `{{METRICS}}` だけである。SQL を自分で書かない。D1 にも Analytics Engine にも
直接触らない。

```
{{METRICS}} list
{{METRICS}} metric <key> <from YYYY-MM-DD> <to YYYY-MM-DD>
{{METRICS}} events <from YYYY-MM-DD>
```

窓の取り方には規律がある。rollup は日単位なので、デプロイ当日は変更前と変更後の両方の traffic を
含む。だから baseline の `window.end` はデプロイ日の前日、measurement の `window.start` は
デプロイ日の翌日にする。1 日を捨てる代わりに、混ざった日を判定に使わない。

`metrics_file` に無い metric は使えない。必要なら journal に「人間が metric を足せば測れる候補」
として書き、その仮説は作らない。metrics_file と loop.yaml を自分で編集しない。編集した PR は
merge gate が拒否する。

## 3. 実装（BUILD の時だけ）

`toshipon-mode` skill を Skill tool で起動し、Feature playbook に入る。触れるのは `loop.yaml` の
`allowed_paths` の中だけである。

`change.paths` に書いた path 以外を触ったら merge gate が PR を拒否する。触る範囲が変わったら
record の `change.paths` を先に直す。

## 4. Ship

1. `{{CHECK}}` を実行する。これが唯一の check 経路であり、gate が走らせるものと同じである。
   赤ければ ship しない。
2. commit する。構造変更と動作変更を別の commit にする。
3. `{{PUSH}}` で push する。これが唯一の push 経路である。`git push` は使えない。
4. `gh pr create` で main に対して PR を開く。body に record id、metric、baseline の値と窓、
   falsification、success を書く。
5. journal のエントリを step 6 の書式で書き、同じ PR に入れる。commit は step 6 の後。
6. `{{MERGE}} <pr>` を実行する。これが唯一の merge / deploy 経路である。

   - exit 0 = マージとデプロイを確認した。stdout の `deploy_version=` を record の
     `ship.deploy_version` に書く。record を `shipped` にする
   - exit 3 = 人間のレビューが必要。record は `building` のまま残す
   - exit 4 = merge window の外。record は `building` で残し、次の tick に任せる
   - exit 1 = checks かデプロイが失敗した。原因を journal に書いて止まる

## 5. KaizenLab

BUILD で canvas を作り、EVALUATE で canvas を閉じて learning を 1 件足す。INTERVIEW で persona と
interview を登録する。project_id は `loop.yaml` の `kaizenlab_project_id`。

KaizenLab を読んで判定を動かさない。state も verdict も record が決める。MCP が失敗したら journal
に書いて先に進む。KaizenLab の失敗で record の state を止めない。

INTERVIEW で surface を観測する手段は、headless では `loop.yaml` の `surface_url` に対する
WebFetch である。ブラウザを動かすのは対話の `/toshipon-mode:loop-tick` の時だけにする。画面を
一度も見ずに書いたインタビューは作文なので、観測できなかったならそう書く。

## 6. Journal

`{{RECORDS_DIR}}/journal.md` に 1 エントリ追記する。何もしなかった tick も書く。日本語で書く
（技術用語は英語）。見出しは `## <date> {{TICK}}`。

前半 5 行は owner の通知にそのまま出る。runner はこの 5 つだけを送る。ラベルは逐語で使う。
翻訳も改名もしない。runner は grep で拾い、一致しない bullet を黙って落とす。

- `- **要約:**` 3 行まで。何を選び、何が分かり、どう判断したか
- `- **行動:**` 1 行。決定表のどの行を実行したか、対象の record id
- `- **計測:**` 1 行。metric、baseline と measurement の値、n。測っていなければ「なし」と理由
- `- **次:**` 1 行。次の tick が取る行動の予測
- `- **要対応:**` 1 行。人間にしかできない作業がある時だけ。無ければ bullet ごと省く

後半は後で読む記録である。通知には出ないので、証拠が必要とする長さで書く。ラベルは逐語で英語。

- `- **Guard:**` health check と paused.flag の状態
- `- **Did:**` 何をしたか。判断の根拠を含む
- `- **Records:**` state が動いた record の id と遷移（`PH-0001 drafted → building`）
- `- **Evidence:**` 計測の生の値と、どのコマンドで読んだか
- `- **Not shipped:**` main に届かなかったもの
- `- **Theatre check:**` terminal record のうち inconclusive の比率。50% を超えたら `要対応`
  にも書く

step 1 で選んだ 1 行だけを実行して、step 6 を書いたら終わる。深さが幅に勝つ。同じ metric に
2 本目の仮説を重ねてはいけない。
