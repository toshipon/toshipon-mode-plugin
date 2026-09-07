### Perf issue

**測定のストーリーはあなたが持つ。計画し、レビューし、数値を検証する。** すべての修正を測定に結び付け、測定の代わりに source を読むことはしない。

1. 一致する driver skill で baseline trace を取る。
2. 仮説を根拠づけるために `how` を使う。実行せずに perf の上限を主張しない。
   ほとんどの修正は 8 つの戦略群のどれかから来る。これらを checklist としてではなく仮説生成器として使う。ある戦略群が試す価値を持つのは、trace がそれが名指しするシグナルを示すときだけであり、8 つすべてを適用するより支配的なコストへの焦点を絞った修正の方が勝る。
   - **Elimination.** 一番安い作業は、そもそも実行されない作業である。hot path を最適化する前に、それが存在する必要があるかを問う。誰も消費しない計算、このユーザーには常に off になっている feature gate、状態を冗長にミラーする同期、「念のため」残された legacy path。trace は何が遅いかを示すのであって、削除可能かどうかは示さない。この戦略群には profiler ではなく `how` パスが必要である。作業を削除することは、当てはまる場合には他のどの戦略群よりも勝る。
   - **Divide and conquer.** 支配的なコストが入力サイズに応じてスケールする。各ピースが触れる量を減らすように作業を分割する（chunk、shard、探索空間を pruning）か、独立したピースを並列に実行する。
   - **Caching.** 同じ計算やフェッチが同一の入力で繰り返される。結果を保存し再利用する。それを無効化するものを、勝利を主張する前に名指しする。
   - **Indirection.** hot path が、もっと安い中間層が吸収できる高価な作業をしている。scan の代わりの index、interactive thread から作業をずらすキュー、より安い実装を差し替えられる handle。critical path から取り除く量がホップを追加する量を上回るときだけホップを追加する。作業を取り除かずに hot path に乗るレイヤーは純粋なコストである。
   - **Batching.** 多くの小さな操作がそれぞれ固定オーバーヘッドを払う（RPC、query、syscall、draw call）。それらをまとめて、バッチごとに 1 回だけオーバーヘッドを払う。
   - **Redundancy.** 待ちが 1 つの遅いインスタンスや試行に依存している。作業を複製し（レプリカ、hedged request、投機的実行）、最も速い結果を採用する。これは追加の負荷と引き換えに tail latency を下げるので、trace が wait が支配的であることを示し、システムに余力があることを示す必要がある。このトレードオフなしの複製は負荷を増やすだけである。
   - **Lazy evaluation.** コストが、使われないか、まだ必要とされない結果にかかっている（boot path での eager な初期化、画面外アイテムのレンダリング）。最初の使用まで作業を遅らせる。
   - **Scheduling.** その作業は必要だが、interactive な瞬間である必要はない。誰も待っていないところに移す。idle callback、boot 後のバックグラウンド warmup、ユーザーが来る前の precompute、フレームの commit 後の cleanup。Lazy（必要になったら後で）とは異なり、Scheduling はしばしば hot moment より *早く*、あるいはその影に隠れて作業を実行する。得られる勝利は体感レイテンシなので、行われた作業全体ではなく interactive path を測定する。
3. trace から修正を計画する。関数境界をまたぐなら、まず `architect` agent を実行する。実装は Agent tool で委譲する（`model: "sonnet"`、hot path が微妙なら `model: "opus"`）。diff をレビューする。修正後の trace を取る。
   [sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) を適用し、次を試す前に各試みを検証する。
4. 成果物を parse し比較する（JSON を sqlite に、diff を取る）。「不明瞭」や誤った surface は合格ではない。フラグを立てる。
5. PR で測定結果を引用する。
6. `/verification-loop` を実行し、`/pr-create` で PR を開く。

web surface では、**web-perf** skill が Core Web Vitals の取得と render-blocking analysis を担う。step 1 と 3 の driver として使う。

**Reply:** baseline の数値、修正後の数値、差分、成果物のパス。
