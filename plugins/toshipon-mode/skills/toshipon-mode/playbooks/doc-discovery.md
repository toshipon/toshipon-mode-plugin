### Doc discovery

**前提はあなたが持つ。集め、台帳に落とし、知らないことに名前を付ける。** 「要求定義を書きたい」「設計書を起こしたい」「まず既存システムを把握したい」向けで、まだ workspace がない場合。成果物はドキュメントではなく台帳である。`facts.md` と初回の `questions.tsv` を作って引き渡す。ドキュメントを書くのは Doc author の仕事である。

台帳と規約は [`references/doc-workspace.md`](../references/doc-workspace.md) にある。読んでから始める。証拠は出典である。出典のない事実は事実ではなく質問である。

1. workspace の置き場所を決める。`doc-workspace.md` の **Where the workspace lives** の順に確認し、最初に当てはまった場所に `<slug>/` を切る。決めたパスと理由を `decisions.tsv` の最初の行に書く。`find . -name questions.tsv` で既存の workspace が見つかったら、それは pickup である。`playbooks/session-pickup.md` を通してから Doc question loop か Doc author に進む。
2. 問いをスコープする。何のドキュメントを、誰が読み、何を決めるために書くのか。読者と決定が言えなければ、書く前にユーザーに聞く。これは実験では決着しない本物の判断である（[never-block-on-the-human](../references/principles/never-block-on-the-human.md) の例外）。答えを `decisions.tsv` に残す。
3. 材料を集める。`doc-workspace.md` の **Source adapters** の順に取り込み経路を判定する。MCP、ブラウザ、ユーザーが貼る、の順。ユーザーが挙げた Confluence ページ、Slack スレッド、規制文書、既存コードを 1 つずつ `sources.tsv` に足し、本文を `sources/` に保存する。取り込めなかった材料は「素材依頼」として reply に列挙する。質問票には載せない。
4. 台帳化を委譲する。材料ごとに Agent tool（`model: "sonnet"`、`run_in_background: true`、ファイルパスのポインタを渡す）を起動し、`facts.md` の候補行を返させる。1 行 1 事実、末尾に出典札、出典で言えないことは書かない。矛盾する事実は両方残し、矛盾を解く質問を起こす。main thread には候補行だけを受け取り、自分で読んで `facts.md` に入れる（[guard-the-context-window](../references/principles/guard-the-context-window.md)）。既存システムがコードとしてあるなら、`how` skill（Explain mode）をかけ、その出力を `kind=code` の出典として `facts.md` に落とす。
5. 規制要件を突き合わせる。ユーザーが挙げた基準（FISC、US-SOX、SOC1、社内規程）ごとに、`kind=regulation` の材料があるか確認する。あれば該当項目を条項 ID 付きで `facts.md` の「外部要件」に抜粋する。なければ「対象となる基準の版と適用項目の範囲」を owner 付きの質問にする。記憶から条項番号を書かない（[boundary-discipline](../references/principles/boundary-discipline.md)）。
6. ギャップを起こす。書く予定の skeleton（`references/skeletons/`）の節を上から順に見て、`facts.md` で埋められるかを問う。埋まらない節ごとに未知を `doc-workspace.md` の **質問票に載せる前に分類する** で分類する。読めば分かるものは step 3 に戻る。試せば分かるものは `playbooks/prototype.md` に送る。人が決めるものだけ `scripts/question.sh <tsv> add <question> <blocks> <owner>` で起こす。`blocks` と `owner` は空にしない。owner が分からない質問は、owner を決めること自体を最初の質問にする。
7. 引き渡す。`decisions.tsv` に集めた材料と起こした質問の要約を 1 行残す。次の playbook を名指しして止まる。open な質問が成果物の骨格を阻んでいるなら Doc question loop、そうでなければ Doc author。

Discovery は読み取り専用である。成果物ドキュメントには手を付けない。集めながら書き始めたくなったら、それは Doc author に進む合図であり、ここで書く理由ではない。

**Reply:** workspace のパス、集めた材料の件数と種別、`facts.md` の事実の件数、owner ごとの質問（推奨回答付き）、素材依頼、次に走らせる playbook。
