### Refactoring

**契約はあなたが持つ。構造は変わるが、挙動は変わらない。** 「refactor」「rename」「extract」「inline」「dedupe」「restructure」「move this module」「tidy up this area」向け。挙動を追加する Feature や、それを修正する Bug fix とは区別する。

挙動の変更をこっそり持ち込む refactor は安全網を失う。cleanup が欠けている feature や本物のバグを明らかにした場合は、それを切り離し、pin した契約に対する構造変更をまず出荷する。redesign は許されるが、そう名指しして Feature にルーティングする。大規模または横断的な構造変更（多くの呼び出し箇所にまたがる migration、多くの subsystem を協調させる reshape）は独自のプランを必要とする。Playbooks セクションの no-match ルールに従って設計するか、**Multi-phase plan** にルーティングする。この playbook は焦点を絞った中規模までの変更向けである。

1. まず挙動契約を pin する。**how** skill を影響を受ける subsystem にかけて契約を学び、構造を動かす前に現在の挙動を捉える characterization test、snapshot、または equivalence harness を書く。harness が「refactor」をチェック可能な主張にする（[prove-it-works](../references/principles/prove-it-works.md)）。その領域にカバレッジがなければ、構造に触れる前に pin を書く。型チェックと lint は pin にならない。
2. [model-the-domain](../references/principles/model-the-domain.md) に従い、コードに欠けている構造に名前を付ける。散らばった bool の代わりの state machine、広がった分岐の代わりの table や registry、繰り返される形状の想定の代わりの typed model、場当たり的な mutation の代わりの reducer。形状がすでに明確でローカルなら、退屈なコードのままにする。reshape は分岐や不正な状態を削除するものであるべきで、間接性を追加するものであってはならない。
3. 目標の形状に名前を付ける。今日ゼロから作るとしたら、現在の要件が最初から基礎だったとしたら、モジュールのレイアウト、型、call graph はどうあるべきかを述べる。目標が関数境界をまたぐなら、move の前に `architect` agent でその形状を並列に設計探索する。
4. 追加より先に引き算する。新しい形状を導入する前に、デッドウェイトを削除し、呼び出し元が 1 つの wrapper をたたみ、冗長な validator を落とし、孤立した参照を除去する（[subtract-before-you-add](../references/principles/subtract-before-you-add.md)）。目標の形状に到達する最小の変更が出荷される（[laziness-protocol](../references/principles/laziness-protocol.md)）。「役に立つかもしれない」投機的な cleanup は revert し、そのまま乗せておかない。
5. 挙動を保存した小さなステップで動かし、それぞれで pin を green に保つ。API の reshape では、すべての呼び出し元を同じ wave で移行し、古い API を削除する。互換性のための shim も、新旧並行の path もなし。すべての rename を実際のファイルに対してスポットチェックする。rename は文字列、prose、back-reference の中の使用箇所を静かに見落とす。機械的な編集は Agent tool で委譲する（純粋に機械的な sweep には `model: "haiku"`、判断が必要な場合は `model: "sonnet"`）。具体的なスコープ（ファイルパス、動かす名前、保持する挙動）とともに。
6. 「コンパイルが通る」ではなく、実際の成果物で挙動が変わっていないことを証明する（[prove-it-works](../references/principles/prove-it-works.md)）。より大きな reshape では equivalence check を実行する。旧と新の出力を diff する script、新しいコードに対して再生される記録済みの baseline、または関連する driver skill を通した一致する surface での smoke run。
7. 変更が見合うことを確認する。成功の尺度は reader load の削減である（[minimize-reader-load](../references/principles/minimize-reader-load.md)）。質問と答えの間のレイヤーが減る、隠れた状態が減る、2 つ目の consumer を持たない間接性が減る。diff がどこかで reader load を下げていないなら、revert する。
8. ストーリーを語る小さく順序付けた commit にリベースする。引き算の commit、そして reshape、そして follow-on cleanup があれば、それぞれが 1 回の revert で元に戻せるようにする。[sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) で形作り、各挙動保存スライスを次に進む前に green に保つ。`/verification-loop` を実行し、`/pr-create` で PR を開く。

**Reply:** 変わった構造、それを保持するために使った pin、equivalence の証明、reader-load の差分、出荷したものと revert したもの。新しい挙動はなし。
