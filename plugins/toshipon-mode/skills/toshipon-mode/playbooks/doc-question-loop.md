### Doc question loop

**未決はあなたが持つ。分類し、まとめて聞き、答えを台帳に戻す。** `questions.tsv` が存在し、`open`、`asked`、`assumed` が残っている場合。「質問票を更新して」「回答をもらったので反映して」「残りの質問を潰して」向け。`/loop` と Session pickup に組み合わせて日をまたいで回す。

台帳と規約は [`references/doc-workspace.md`](../references/doc-workspace.md) にある。台帳が真実であり、トランスクリプトから再導出しない。

1. `scripts/question.sh <tsv> list` で全件を読む。`open`、`asked`、`assumed` を分けて把握する。`asked` は `asked_at` からの経過を見る。
2. `open` を再分類する。**質問票に載せる前に分類する** をもう一度かける。読めば分かるものは自分で材料を読みに行き、`sources.tsv` に足し、`answered` にする（`evidence` に `S<n>`）。試せば分かるものは `playbooks/prototype.md` で決着させる。人が決めるものだけ残す。分類の結果、聞く必要がなくなった質問は `dropped` にし、理由を `answer` に書く。
3. owner ごとにまとめて聞く。`scripts/question.sh <tsv> md --owner <name>` で表を作り、各質問に「なぜ今必要か」（`blocks` の中身）と推奨回答を添える。owner は判断ではなく反応でよくなる。送るのはユーザーである。Slack MCP があっても、社内の関係者への質問は既定でユーザーが送る。送信は外部への行動で取り消せないので、Autonomy の「確認なしで進める」の例外とする。ユーザーが「送っていい」と言えば送る。送ったら `set <id> status asked`。
4. 回答を戻す。回答のテキスト（Slack のスレッド、会議のメモ、メール）を `kind=slack` か `kind=meeting` の材料として `sources.tsv` に取り込み、`set <id> answer <内容>`、`set <id> evidence S<n>`、`set <id> status answered` の順で更新する。回答が新しい未知を生んだら分類して `add` する。回答が `facts.md` の既存の事実と矛盾するなら、訂正行を足して矛盾を残さない。
5. 阻んでいるのに回答が来ないものは仮置きする。推奨回答を `answer` に書き、`status assumed` にして進める。成果物では `[Q<n> 仮]` として見える。待つことはコストが高く、仮置きは可逆である（[never-block-on-the-human](../references/principles/never-block-on-the-human.md)）。何を仮置きしたかを `decisions.tsv` に 1 行残す。
6. 反映する。`answered` と `assumed` のうち成果物に書き込んだものは `set <id> incorporated_in <節名>` と `set <id> status incorporated`。`facts.md` にも対応する事実行を足す。成果物の本文と `## 未決事項` を台帳に合わせて更新する。
7. `scripts/doc-check.sh <workspace>` を走らせる。成果物が参照する `Q` の状態と `## 未決事項` の整合、`S` の存在を確認する。エラー 0 まで直す。
8. 判定する。`open` と `asked` のうち、まだ成果物の骨格を阻んでいるものが残っていればこの playbook を続ける。残っていなければ Doc author に渡す。`/loop` で回す場合は 1 反復につき `decisions.tsv` に 1 行。

**Reply:** 閉じた質問（誰の回答で）、新しく開いた質問、仮置きしたものとその内容、owner ごとの待ち（経過日数付き）、次は Doc author か継続か。
