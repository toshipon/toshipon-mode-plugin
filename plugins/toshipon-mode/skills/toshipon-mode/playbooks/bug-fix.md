### Bug fix

**このタスクはあなたが持つ。計画し、レビューし、検証する。** 調査と修正は subagent に委譲し、自分は先頭に立つ。

科学的であること。出荷するすべての行は runtime の証拠に遡れる。「役に立つかもしれない」belt-and-suspenders は仮説であって修正ではない。出荷しない。証拠が仮説を反証したら、それが動機づけたものを revert する。証拠が正当化する最小の変更を出荷し、それ以上は出荷しない。証拠が trace である Perf でも同じ規律を適用する。

1. Non-negotiables にある driver skill 経由で、一致する surface 上で自分自身で再現する。ユーザーに repro を渡さない。ユーザーに聞くよう指示する debug や instrumentation の protocol も、この規律を上書きしない。あなたが instrumented runtime を駆動する。ユーザーに聞くのは、driver がターゲットに届かない具体的な理由がある場合のみで、しかもそれを driver で行けるところまで駆動した後だけである。直接再現しない場合は強制する。トリガーを合成する、条件を絞る、発火するまで instrument する。再現できないバグは、直ったと証明できない。
2. 原因を二分探索する。候補となる仮説を立て、1 つが生き残るまで排除していく。影響を受ける subsystem には `how` を、regression の履歴には **why** skill を種として使う。各パスで、残っている問題空間を最も削る分割を選び、runtime の証拠を得て、排除する。プログラム状態が不明瞭なときは instrumentation や logging を追加し、コードが動く様子を読む。推測しない。長く粘るハントは Claude Code 組み込みの `/loop` で駆動する。step 3 の design fan-out の前に、生き残った *mechanism* を runtime の証拠で確認する。もっともらしいが未確認の原因の上に設計を組むと、全会一致で間違っていながら本物の原因が 1 つ隣の subsystem にあるということが起こり得る。
   [fix-root-causes](../references/principles/fix-root-causes.md) に従って各症状を根本原因まで遡る。
3. 修正を計画する。関数境界をまたぐなら、まず `architect` agent を実行し並列で設計を探索する（`../references/common/agents.md` 参照）。実装は Agent tool で委譲する。通常の修正には `model: "sonnet"`、原因が微妙だったり意図が曖昧だったりする場合は `model: "opus"`、具体的なスコープを添えて。diff は自分でレビューする。
4. 同じ surface で検証する。元の repro が通るようになる。「不明瞭」や誤った surface は合格ではない。フラグを立てる。unit test はブランチの挙動を示すのであってバグの不在を示すのではない。これは [prove-it-works](../references/principles/prove-it-works.md) である。
5. 失敗する repro が git の履歴で修正より先に来るように commit を分ける。diff がストーリーを語る。バグが安価な local test path を持つ場合の failing-test-first の cadence については、リポジトリの **tdd** skill（`${CLAUDE_PLUGIN_ROOT}/skills/tdd/SKILL.md`）を参照。テストが高価だったり integration 重かったり不明瞭だったりする場合はスキップする。
   これは [sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) の典型例であり、失敗するテストを先に、その上に修正を乗せる。
6. `/verification-loop` を実行し、`/pr-create` で PR を開く。

Investigation は `how` と `why` を並列の subagent として fan out する。

**Reply:** 何が壊れていたか、根本原因、修正内容、どう検証したか。決め手となる失敗と成功の出力を、主張とカウントに絞って引用する。
