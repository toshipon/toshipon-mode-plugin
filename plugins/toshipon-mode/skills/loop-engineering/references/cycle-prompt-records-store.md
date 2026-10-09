
## 記録は外部の記録面にある（records_pull / records_push）

この repo の `loop.yaml` は `records_pull` と `records_push` を設定している。上の手順のうち、record と
journal を commit する箇所はすべてこの節が上書きする。

- `{{RECORDS_DIR}}` の record（`hypotheses/`、`measurements/`、`journal.md`）は、runner が tick の前に
  外部の記録面から書き出した working copy である。git は無視している。読み方も書き方も今までと同じで、
  その場で編集する
- record と journal を **commit しない**。`git add` に record の path を渡さない。tick が終わった後に
  runner が working copy を外部の記録面に保存する
- record だけが動いた tick（RECONCILE、ADVANCE、EVALUATE、INTERVIEW、record だけの GUARD）は
  **commit も push も PR もしない**。record と journal を書いたら終わる
- BUILD は **コードだけ** を commit し、PR に入れる。record の `building` への遷移、`ship.pr`、
  merge gate の出力から書く `ship.deploy_version` は working copy に書く
- EVALUATE の計測 JSON は `{{RECORDS_DIR}}/measurements/PH-NNNN.json` に書く。commit はしない
- journal のエントリは step 6 の書式どおりに `{{RECORDS_DIR}}/journal.md` の末尾に追記する。書く条件
  （state が動いたか、コードを変えたか）も同じである。runner は追記された差分から通知を作るので、
  既存のエントリを書き換えない
- `paused.flag` は record ではなく git にある。GUARD で置くときは今までどおり commit する
- record の不変条件（record-schema.md）は、保存するときに `records_push` が検査する。git の diff は
  もう record を見ていないので、破れた record は保存されず、その tick の仕事は残らない。terminal の
  record と、drafted を出た record の `falsification` と `success` には手を触れない
