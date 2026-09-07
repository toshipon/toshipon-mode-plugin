# Principles Index

タスク開始時にこの index を読み、該当する原則の本文を読んでから適用する。適用した場合は応答で「原則名 + それが変えた具体的選択」を明示する（決定を伴わない引用は無効）。

## Core

- **[Laziness Protocol](laziness-protocol.md)** — 最小のコードと複雑さで最大の結果を狙う。削除を優先し、呼び出し階層を浅く保つ。リファクタリング時や、抽象化・レイヤー・新しいシグナルの配線を追加したくなった時に適用する。
- **[Subtract Before You Add](subtract-before-you-add.md)** — システムを進化させる時は先に複雑さを取り除き、その上に構築する。追加・リファクタリング・書き直しの順序を決める時に適用する。
- **[Minimize Reader Load](minimize-reader-load.md)** — 保守性とは読み手が理解するために払うコスト。トレースすべきレイヤー数と保持すべき状態量の両方を減らす。追跡しづらいコードのレビューや整形時に適用する。

## Architecture

- **[Model the Domain](model-the-domain.md)** — 実際のドメインを条件分岐に散らばせず、データ構造にエンコードする。ステートフルなロジックを書く時や、分岐・形状の前提がファイル間で繰り返される時に適用する。
- **[Boundary Discipline](boundary-discipline.md)** — バリデーション・型の絞り込み・エラーハンドリングはシステム境界に集中させ、内部コードは無条件に信頼する。バリデーション・エラーハンドリング・フレームワークアダプタを配線する時に適用する。

## Verification

- **[Prove It Works](prove-it-works.md)** — プロキシや自己申告、「コンパイルが通った」ではなく、実物を直接確認して検証する。タスク完了後、完了を宣言する前に適用する。
- **[Fix Root Causes](fix-root-causes.md)** — 症状を覆い隠さず、根本原因まで遡って修正する。デバッグ時に適用する。
- **[Sequence Verifiable Units](sequence-verifiable-units.md)** — 検証可能な小さな単位に作業を分割し、各単位を確認してから次に進む。コミットや PR の積み方にも同じ規律を適用する。スイープ・マイグレーション・類似編集の連続作業や、コミット・PR の構成を決める時に適用する。

## Delegation

- **[Never Block on the Human](never-block-on-the-human.md)** — 人間は非同期に監督する。可逆な作業では「〜していいですか」と聞かずに実行し、結果を提示して後から修正してもらう。可逆な作業で確認を取りたくなった時に適用する（不可逆な操作は確認必須のまま）。
- **[Guard the Context Window](guard-the-context-window.md)** — コンテキストウィンドウは有限でセッション内では再生されない。大きな出力はサブエージェントに逃がし、メインスレッドには要約だけを残す。コンテキストが埋まってきた時（大量の出力、長いファイル、繰り返しの読み込み、ファンアウトする計画）に適用する。

## Meta

- **[Encode Lessons in Structure](encode-lessons-in-structure.md)** — 繰り返し発生する修正は、文章による指示ではなく、lint・メタデータフラグ・ランタイムチェック・自動化などの仕組みにエンコードする。同じ指示を 2 回書いていることに気づいた時や、繰り返しの訂正に気づいた時に適用する。
