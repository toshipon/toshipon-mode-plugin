### Perf issue

**測定のストーリーはあなたが持つ。計画し、レビューし、数値を検証する。** すべての修正を測定に結び付け、測定の代わりに source を読むことはしない。

1. 一致する driver skill で baseline trace を取る。baseline と、その後のすべての数値を [`../references/benchmark-checklist.md`](../references/benchmark-checklist.md) で吟味する（[explain-the-number](../references/principles/explain-the-number.md)）。
2. 仮説を根拠づけるために `how` を使う。実行せずに perf の上限を主張しない。
   performance mantras を、安いものから順に試す。
   1. やらない。結果を誰も使わない作業は、安くするのではなく止める。
   2. やるが、二度はやらない。
   3. 減らしてやる。
   4. 後でやる。
   5. 見ていないときにやる。
   6. 並行してやる。
   7. 安くやる。

   前の mantra で目標に届いたら、そこで止める。
3. trace から修正を計画する。関数境界をまたぐなら、まず `architect` agent を実行する。実装は Agent tool で委譲する（`model: "sonnet"`、hot path が微妙なら `model: "opus"`）。各 subagent の `effort` は、SKILL.md の Delegation 節にある「仕事に model と effort を合わせる」に従い、仕事の難しさに合わせて選ぶ。diff をレビューする。修正後の trace を取る。
   [sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) を適用し、次を試す前に各試みを検証する。
4. 成果物を parse し比較する（JSON を sqlite に、diff を取る）。「不明瞭」や誤った surface は合格ではない。フラグを立てる。
5. PR で測定結果を引用する。
6. `/verification-loop` を実行し、`/pr-create` で PR を開く。

web surface では、**web-perf** skill が Core Web Vitals の取得と render-blocking analysis を担う。step 1 と 3 の driver として使う。

**Reply:** baseline の数値、修正後の数値、差分、成果物のパス。
