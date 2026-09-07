### Feature

**設計はあなたが持つ。計画し、レビューし、検証する。** 実装は委譲する。自分は先頭に立つ。

1. Hypothesis gate。設計作業の前に、要求に対して **hdd:hypothesis-first** skill を実行する。feature が賭けている仮説の名前、それが検証済みかどうか、どんな証拠によるかを述べる。costly な feature に対する未検証の仮説は、まず `hdd:verify` か `hdd:grill` にルーティングする。そのことを述べて止まり、未検証の賭けの上に構築しない。検証済みか、覆すコストの低い賭けであれば進み、仮説とその証拠を reply に記録する。スキップした場合は `hypothesis gate skipped: <reason>` としてリストに残す。
2. 影響を受ける subsystem に `how` をかける。
3. `architect` agent を実行し並列で設計を探索する（`../references/common/agents.md` 参照）。スキップは `architect skipped: <reason>` としてリストに残す。設計判断を黙って実装に紛れ込ませない。
4. throughput checkpoint を 4 つの todo 項目として書く。本当に当てはまらない次元（単一ファイル、fan-out なし）も、削除するのではなく `n/a: <reason>` を付けて項目を残す。
   - **Blocking first steps.** gate は fan-out の前に走る。
   - **Independent workstreams.** 独立したファイル・サービス・レイヤーは並列化する。共有される書き込みは直列化する。
   - **Shared mutable state.** 既定では対象を分割し、並行するワーカーが同じファイル・ブランチ・キー・オブジェクトに書き込まないようにする。本物の不変条件のときだけ直列化する。
   - **Smallest safe decomposition.** 1 人のワーカーが最善なら、その理由を述べる。
5. コード書きは Agent tool で委譲する（通常の作業には `model: "sonnet"`、横断的な設計・厄介な並行性・繊細なアルゴリズムには `model: "opus"`、些末な機械的編集には `model: "haiku"`）。具体的なスコープ、つまりファイルパス、名前を付けたデータ形状とその組織構造を [model-the-domain](../references/principles/model-the-domain.md) に従って決める（散らばった bool の代わりの state machine、分岐の代わりの table/registry、繰り返される形状の想定の代わりの typed model、delegate がロジックを書く前に選んでおく）、そして成功基準を添える。diff は自分でレビューする。実装が複数の妥当な形状を許す場合（エラーハンドリング、抽象化レイヤー、テスト構造）は、代わりに **arena** skill 経由で委譲し、runner に選択肢を出させ、cross-judge に選定を守らせる。必須であり、理由付きのスキップは許されない。Laziness Protocol もこれを上書きしない（得られるのは行数の節約ではなくレビューの分離である）。あなた自身が subagent であっても subagent を起動してよい。「アプリが小さい」も「subagent は subagent を起動できない」もどちらも誤りである。spawn を禁じられた subagent は、同じレビュー分離をもって自分で diff を持つことでこれを満たす。ネストした agent 待ちの「standing by」返信は許されない。コメントは **Comments** に従う。surgical な編集、upstream から派生したファイルは source に対して再照合する。共有 primitive の改善はすべての consumer に展開し、それぞれ検証する。commit は惜しまず行う。
6. 一致する surface で検証する。「不明瞭」や誤った surface は合格ではない。フラグを立てる。
7. 小さく順序付けた commit にリベースする。follow-up は stack する。[sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) を使い、各小さな unit をビルドし、検証し、commit してから次に進む。構造変更と動作変更は `${CLAUDE_PLUGIN_ROOT}/skills/commit-rules/SKILL.md` に従い別々の commit に保つ。
8. 設計に議論の余地がある場合は、出荷前に `interrogate` する。
9. `/verification-loop` を実行し、`/pr-create` で PR を開く。

コードが結合した作業（1 つの feature、1 つの migration）は、checkpoint をインラインに持つ単一の owner に渡し、その owner が blocking phase の後に内部で fan out する。親レベルの fan-out は、独立した成果物を生む slice（監査、subsystem 横断の調査、競合する実験）向けである。checkpoint は phase の境界で書き直す。interrupt を連鎖させるのではなく、新しい owner を spawn する。

**Reply:** 仮説とその証拠、何を作ったか、何を選びなぜか、未解決の判断。設計の代替案には table を使う。
