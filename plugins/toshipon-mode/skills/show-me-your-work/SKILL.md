---
name: show-me-your-work
description: 長時間・無人実行の作業について、レビュー可能な意思決定ログ（what/why/evidence/result の TSV）を残す。既定はローカル保存で、レビュアーが根拠を必要とする時だけコミットする。/show-me-your-work での明示呼び出し、自律・多段階の実行、離席後にレビューされる作業に使う
disable-model-invocation: true
---

# Show me your work

人が後から確認する作業では、意思決定の記録があれば、作業をやり直したりトランスクリプト全体を読んだりせずに、何が決められ、なぜそうしたか、どんな根拠に基づいたかを再構成できる。記録の一貫性を保ち、将来の agent がそれを見つけられるように、正典となるログを 1 つだけ保つ。

## The format

1 決定 1 行の単一の TSV ファイル。TSV を使う理由は、GitHub がそれをソート可能な表としてレンダリングすること、`column -s$'\t' -t` やスプレッドシートで読めること、そして 1 コマンドで行を追記できることである。セルは 1 行に収める。evidence はプロースではなくポインタにする。

新しいログを始めるには `references/decision-log-template.tsv`（ヘッダー行）をコピーする。列は次の通り。

- **ts。** ISO8601 のタイムスタンプ。時系列の軸になる。
- **phase。** フェーズやワークストリーム。
- **decision。** 何を選んだか、何をしたかを 1 行で。
- **why。** 平易な言葉での理由。ある原則が判断を導いたなら、専門用語のタグではなく平易にそう書く（`explored options first, this was a one-way door` のように）。
- **evidence。** それを裏付けるリンクかパス。コミットの SHA、PR 番号、`file:line`、あるいは artifact・トレース・スクリーンショットのパス。段落にはしない。
- **result。** 結果や状態を示す述語。`tests green`、`reverted`、`pixel-diff 0`、`INCONCLUSIVE`、`open` のように。

以下は、レビュアーが一目で読めるよう平易に書いた例である。あくまで説明用であり、これらの行を実際のログにコピーしないこと。

```
ts	phase	decision	why	evidence	result
2026-05-24T09:02:00Z	frame	counted the work first, about 100 components and roughly 75 hours	wanted to know the size before starting a long run	commit 3a9f1c2	found 5 things to sort out before starting
2026-05-24T09:40:00Z	harness	took screenshots of the old version before changing anything	so we can compare old against new and catch any visual change	scripts/snapshot.sh, baseline/	saved 120 reference screenshots
2026-05-24T11:15:00Z	widget	moved the widget styles over without changing how it looks	keep the change small and the result identical	commit 7c21e0a, pixel-diff 0	looks identical, tests pass
2026-05-24T12:30:00Z	widget	threw out a helper's work because its screenshots were blank	checked the real files instead of trusting its summary	worktree reset	reverted, tightened the instructions for next time
```

## Logging a row

チームメイトに何をしたか伝えるように各エントリを書く。平易な言葉、具体的な行動。AI 語彙や抽象的な専門用語は使わない（**unslop** スキルはログの文章にも適用される）。レビュアーは、解読しなくても各行を理解できるべきである。

行が整形されるよう、ヘルパーを使う。`scripts/log.sh <logfile> <phase> <decision> <why> <evidence> <result>`。これは `ts` を刻印し、初回利用時にヘッダーを書き、余分なタブや改行を取り除き、`=`、`+`、`-`、`@` で始まるセルにはシングルクォートを前置する。これにより、レビュアーがスプレッドシートでログを開いたときに数式が実行されてしまうことを防ぐ。素の `printf` で行を追記することもできるが、セルが生成テキストやユーザー入力由来である場合は同じバイト列に注意する。

意思決定のポイントとチェックポイントを記録し、すべての行動は記録しない。選んだ分岐、検証結果を伴って完了した単位、トリガーを伴う方針転換や revert、表面化したブロッカー、修正したゲートなど。ループ実行では、1 反復につき 1 行。取るに足らない自明なことは省く。

## Where it lives

既定では、このログは作業用の artifact であり、コミットしない。作業ディレクトリの `decisions.tsv` に置くか、複数の作業を同時に走らせている場合は `.audit/<task-slug>.tsv` に置き、git には含めない。ほとんどの作業ではコミット済みの記録は不要である。ローカルのログだけでも実行の誠実さは保たれ、終わったら破棄してよい。

コミットするのは、レビュアーが結果を信頼するためにその記録を必要とするほど野心的な作業のときだけである。大規模なクロス言語の移植、数週間にわたる移行、結果への確信を「示す」必要がある作業などである。コミットされたログは PR 内で表としてレンダリングされる。

## Rules

- 1 行は 1 つの決定かチェックポイント。1 行に収まらないなら、その決定はまだ明確になっていない。
- 追記のみ。誤った判断は、それを上書きする新しい行を追加する。履歴の編集や削除は決してしない。
- 手作りの使い捨てスクリプトより、コミットされたスクリプトが生成した証拠を優先し、レビュアーが再実行できるようにする（Encode Lessons in Structure principle、${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/references/principles/encode-lessons-in-structure.md）。

## Audit the log against the transcript

作業を渡す前、実行の最後に、ログが真実を語っているか確認する。この実行のトランスクリプトを、Claude Code のプロジェクトごとのトランスクリプトディレクトリ `~/.claude/projects/<encoded-cwd>/` の下で読む。`~/.claude/projects/` を横断して glob しないこと。関係のない他の非公開チャットまで読んでしまう。ログを実際に起きたことと突き合わせる。

- すべての行が実際の行動に対応している。作り話や願望的なエントリは削る。
- 各行の evidence は実際に解決でき、その行が主張する内容を示している。
- 作業の形を左右したが記録されていない分岐・方針転換・断念したアプローチはギャップである。追加する。
- 埋め草は削る。誰も監査しないような行なら、そこにある意味がない。

物語ではなくログを直す。作業がある行の主張から逸脱していたなら、その行が間違っている。

## Cross-model review of the trail

作業を渡す前に、実際に作業を行ったモデルとは異なるモデルファミリーのサブエージェントを（Agent tool の `model` オプションで）必ず起動する。自己レビューでは代替にならない。ここで重要なのは、自分では持ち込めない新鮮な視点である。このサブエージェントは監査記録と実行のトランスクリプトを読み、ユーザーが注意を払うべき点を指摘する。作業のやり直しではなく、最適でない点やリスクのある点のスキャンである。

- 根拠が弱い、あるいは存在しないまま記録された決定。
- スキップされた検証ステップ、またはトランスクリプト上に証拠のないまま主張された検証。
- 振り返ると危うく見える選択（時期尚早、スコープの肥大化、症状への対症療法)。
- ユーザーが流し読みでは見逃しがちなギャップ。

記録が生成された実行のすべての返信は、"Attention" セクションで終える。まずレビュアーのモデル名を単独の行に書き（`reviewed by <model>`）、続けて各指摘を具体的な行や場面へのポインタとともに列挙する。"No flags" は有効な値だが、モデル名の省略は許されない。self-audit はログが真実を語っていたかを問うものであり、これはログが真実であっても、それでもユーザーが精査すべき点を問うものである。

## Reviewing the trail

上から下へ読み、evidence のポインタをたどり、抜き取りで確認する。GitHub はコミットされた TSV を表としてレンダリングする。`column -s$'\t' -t decisions.tsv` はターミナルでレンダリングする。evidence が解決しない行や、result が未検証の行は、監査がギャップを捉えているということである。

## Composing this skill

他のスキルは、独自の監査記録を発明するのではなく、監査記録をここに委ねる。名前で参照し、フォーマットの管理はこのスキルに任せる。列を再定義しない。
