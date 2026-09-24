---
name: technical-writing
description: Diátaxis 構造・Google 開発者スタイル・STE 指示規則・Global English 構文を重ねた技術文章の執筆・レビュー基準。/technical-writing での明示呼び出しや、ドキュメント・RFC・README・PR 説明・コミットメッセージの執筆時に使う
disable-model-invocation: true
---

# Technical writing

目標は、疲れたエンジニアが一度読んだだけで理解できる文章を書くことである。4 つの層がそこへ導いてくれる。それぞれの層に 1 つずつ問いがある。これはどんな種類のドキュメントか、文はどう読者に語りかけるか、それぞれの文はどれだけの情報を運んでいるか、そしてどの文も二通りに読めないか。4 つすべてを適用する。

4 つの層の上に、3 つのルールがある。

- **仕事をしていない単語はすべて削る。** その単語がなくても文が成立するなら、その単語は削る。"In order to" は "to" である。"It is important to note that" は何もない。
- **短くて日常的な言葉を使う。** "utilize" ではなく "use"。"facilitate" ではなく "help"。"perform" ではなく "do"。長い単語は、その長さに見合う精密さを提供して初めて使う価値がある。
- **あるルールが文を悪くするなら、別の方法で文を直すか、そのままにする。** ルールは読者に仕えるものである。すべてのルールに従っていて、機械が書いたように聞こえる文は失敗している。

コードベース自体が用語集である。実際のシンボル名・ファイル名・フラグ名・コマンド名を書く。類義語や説明的な言い換えは使わない。

専門用語を作らない。開発者が口に出して言う言葉を使う。"move"、"delete"、"a budget that only decreases" であって、"evacuate"、"ratchet"、"endgame" ではない。命名されたパターンでも、そのドキュメントで初出時に意味を説明していれば問題ない。新たにこの手の言葉を見つけたら、`unslop` の abstract-metaphor ルールに置き換え候補とともに追加する。

## Vary the rhythm

各層はドキュメントが何を言うか、それぞれの文がどれだけの情報を運ぶかを決める。だが、その全部に従っていても、すべての文が短く刈り込まれ、視点がどこにもなく、具体性が何もなければ、機械が書いたように読めてしまう。

- 意図的に文の長さを混ぜる。短い文は要点を突く。ゆっくり時間をかける長い文は、事実をその条件や帰結とともに運ぶ。
- 「1 文に 1 つの考え」は「1 つの長さの文だけを使う」という意味ではない。2 つの考えを運ぶ文は分割する。1 つの考えを運ぶ長い文はそのまま残す。
- モードが許す場面では見解を持つ。explanation はトレードオフを検討するものなので、賛否を並べるのではなく、それについて自分がどう考えるかを述べる。reference は乾いたままでよい。
- 無機質より具体的であること。"schema changes can cause issues" ではなく "a column rename fails the build" のように。

## Pick the mode first (Diátaxis)

1 つのドキュメントには 1 つのモード。2 つの問いでモードが決まる。その内容は行動（doing）を導くのか、理解（thinking）を導くのか。そして、それは学習のためのものか、作業のためのものか。

- 行動 + 学習: **tutorial**。
- 行動 + 作業: **how-to**。
- 理解 + 作業: **reference**。
- 理解 + 学習: **explanation**。

このコンパスは、ドキュメント全体にも、1 つの文にも使える。自分が何を書いているのか分からなくなったら、これに立ち返る。この判断は勘に頼るとよく外れる。

**Tutorial: 実践による学習。** あなたは教師である。学習者の成功はあなたの責任であり、学習者の責任ではない。冒頭では学習者が何を「学ぶ」かではなく、何を「作る」かを述べる。すべてのステップは、早く頻繁に、目に見える結果を生む。学習者が何を目にするはずかを伝える。期待される出力、プロンプトの変化、ログの行など。説明は 1 節とリンクに絞る。教える側の間（ま）は授業の流れを断ち切る。具体的であり続ける。「we」を使い、コマンド形式で書く。"First, do x. Now, do y." のように。

**How-to: ゴールまでの手順。** 機械にできる操作ではなく、人が抱える問題を解決する。読者の力量を前提にする。教えることは省く。行動のみ。脱線なし、背景説明なし、それ自体のための網羅性もなし。それらの代わりにリンクを使う。分岐や判断は許容する。"If you want x, do y." のように。ガイドにはタスク名をつける。"Radar array calibration" ではなく "How to calibrate the radar array" のように。

**Reference: 参照のための事実。** 記述する。記述するだけ。指示も、説得も、意見もなし。乾いていて、網羅的で、確信を持って書く。事実、選択肢、制限、エラーをヘッジなしで述べる。説明対象の構造をそのまま映し、コードとドキュメントを並行してたどれるようにする。読者が期待する場所に情報を置く。可能ならコードから生成し、内容が常に正しい状態を保つ。

**Explanation: 理解と理由。** 1 つの範囲に絞ったトピックで、製品から離れても読める内容にする。どの見出しも、頭の中で暗黙の "About..." を付けても違和感がないようにする。実在する why の問いを起点にする。設計判断、経緯、制約、代替案などの文脈を与える。意見が許されるのはここだけである。

モードを混ぜない。tutorial の中に reference の表を入れない。reference の中に tutorial 的な手取り足取りの説明を入れない。how-to の中で議論しない。代わりに分割してリンクする。

Source: diataxis.fr, fetched 2026-07-18.

## Write sentences to the reader (Google developer style)

- 読者を "you" と呼びかけ、現在形で語る。"will" は本当に将来起きることにのみ使う。
- 誰が何をするかを述べる。"is checked" ではなく "the compiler checks" のように。受動態でよいのは、動作主が不明であるか本質的でない場合のみ。
- 指示はコマンド形式で書く。"Click Submit." のように。事実は平叙文で述べる。"should be done" は使わない。
- 条件を指示の前に置く。"To delete the document, click Delete." 読者は自分に関係ない部分を読み飛ばせる。
- よくあるケースを先に、例外はその後に置く。
- 知識のある友人のように語る。バズワードなし、比喩表現なし、指示文で "please" を使わない。手順の中で "simply"、"easy"、"quickly" は決して使わない。それが簡単なら、読者はここにいないはずである。
- 事前の予告（"we will soon support..."）はしない。連続する文を同じフレーズで始めない。
- 不自然に感じる文は声に出して読む。それでも不自然なら書き直す。
- リンクにはリンク先が分かる言葉を使う。ページタイトルか短い説明にする。"click here" は絶対に使わない。リンクの外に文脈の 1 文を足す方が、リンク先に文脈を任せるより望ましい。
- 見出しはトピックではなく要点を運ぶ。"Modes" ではなく "Pick the mode first" のように。センテンスケースを使う。タスクの見出しは動詞句のみ（"Create an instance"）。概念の見出しは名詞句にする。1 ページに h1 は 1 つ、レベルの飛ばしなし。
- 手順には番号付きリスト、それ以外には箇条書きを使う。リストの前には完全な文を置く。項目は並列に揃える。
- コードはコードフォントで、UI 要素は太字で表す。シリアルカンマを使う。"etc." は使わず、リストが部分的であることを事前に伝える。

Source: developers.google.com/style, fetched 2026-07-18.

## Make statements load one at a time (STE rules)

- 指示は 1 文に 1 つ。それ以外の文でも 1 文に 1 つの考え。
- 指示文は約 20 語、それ以外の文は約 25 語を超えたら分割する。
- 警告や条件は、それが守るステップより前に置く。"If hot oil touches your skin, injuries can occur." のように。
- "the" や "a" は省略しない。"Remove backup file" は二通りに読める。"Remove the backup file" なら一通りにしか読めない。
- それぞれの単語には 1 つの意味・1 つの役割だけを与え、それを守る。"check" が「調べる」を意味するなら、「抑制する」の意味では使わない。
- 1 つの動作には 1 つの単語だけを選び、それを使い続ける。あるところでは "start"、別のところでは "initiate" のように使い分けない。
- 手順は直接的なコマンドとして書く。ナレーションにせず、受動態にもしない。"the component must be installed" ではなく "Install the component" のように。
- 可能な限り "-ing" 形の単語を避ける。文法上の役割を多く持ちすぎ、誤読を生みやすい。

Source: asd-ste100.org (Issue 9, 2025), fetched 2026-07-18. 番号付きのルールと辞書は仕様書の PDF にある。上記の原則は、そこから転用できる中核部分である。

## Leave no sentence open to two readings (Global English)

- "only" や "not" のような単語は、それが修飾する単語のすぐ隣に置く。"only fails on growth" と "fails only on growth" は意味が異なる。
- 長い名詞の連なりは分解する。"the proto import budget check script" は "the script that checks the proto-import budget" のようにする。
- "it"、"they"、"this" はすべて、指す対象が 1 つに定まるようにする。迷ったら名詞を繰り返す。"this" や "which" で節全体を指すことは決してしない。
- 動詞を省略しない。"Phase 1 moves the converters and Phase 2 the runtime" では Phase 2 に動詞がない。動詞を与える。
- 構造を示す小さな単語は残す。"Ensure that the switch is off" では "that" が文を一通りにしか解釈できないようにしている。明確さを語数の節約と引き換えにしない。
- 誤読を防ぐ場合は、並列の中でも冠詞を繰り返す。2 つのものを指すなら "the client and host" ではなく "the client and the host" とする。
- 文が二通りにグループ分けできる場合、"and" や "or" がどの部分をつないでいるかを明示する。"Both...and"、"either...or"、"if...then" は無料で使える曖昧さ回避手段である。
- セミコロンではなくピリオドを使う。em dash は新しい文に置き換える。
- 括弧内のテキストは、完全な文法単位にするか、独立した文にする。複数形を "(s)" で表さない。
- スラッシュは使わない。"a/b" や "and/or" ではなく "a, b, or both" と書く。
- 1 つの対象には、どこでも 1 つの名前で呼ぶ。1 つのものを "the gate"、"the ratchet"、"the budget check" と呼び分けるドキュメントは、3 つの異なるものを教えているように見える。変わっていない文を編集のたびに言い換えるのも同じコストがかかる。変わっていないものは書き換えない。
- イディオム、口語表現、ラテン語の略語、比喩は避ける。ネイティブでない読者、翻訳者、そして agent は、いずれも平易な構文を最も正確に解析する。

Source: Kohl, The Global English Style Guide (SAS Press). Guideline text fetched from the Internet Archive and the SAS sample chapter, 2026-07-18.

## Voice and repo specifics

- このスキルが扱うすべてのドキュメントに **unslop** スキルを適用する。AI 語彙、フィラー、ヘッジ表現、フォーマット上の tell といった slop パターンのカタログはそのスキルが管理している。
- PR の説明文やコミットメッセージも文章である。Diátaxis を除くすべての層がこれらにも適用される。
- 製品 UI の文字列はドキュメントではない。それらには自分のプロダクトのコピーガイドラインを使う。
- コードスニペットはタブでインデントする。実在するパスと実在するシンボルを書く。件数やツリー構造に関する主張は、それを反映するコミットの時点で真であるようにし、それを再生成するコマンドを併記する。

## Worked example

Before:

> Configuration of the proto import ratchet budget script parameters is performed via budget.json. Note that it's important to remember that running with --write, which updates the committed budget to reflect the current count, should only be done when lowering it. If exceeded, CI fails.

After:

> `budget.mjs` reads the committed budget from `budget.json` and counts the files that import protos. If the count exceeds the budget, CI fails. Run `budget.mjs --write` only to lower the budget.

各層ごとの修正点は次の通りである。"configuration is performed" は "`budget.mjs` reads" になり、誰かが何かをする形になった（Google）。"Ratchet" は消えた。スクリプトの実際のファイル名が命名を担う（jargon ルール）。5 つの名詞が連なった文字列は平易な節に分解された（Global English）。ヘッジ表現の "note that it's important to remember" は削除された（仕事をしていない単語はすべて削る）。失敗条件は、それが説明するステップより前に移動した（STE）。埋もれていた "should only be done when lowering" は、"only" をその動詞の隣に置いたコマンド文になった（STE）。"If exceeded" には主語（count）が与えられた（Global English）。
