---
name: unslop
description: あらゆる文章から AI 特有の癖（AI tell）を取り除き、人間らしい声を加える。常に適用する
---

# Unslop

AI 特有のパターンを取り除き、人間らしい声を加えるようにテキストを編集する。

## Process

1. 以下のパターンをスキャンする。
2. 書き直す。意味を保ち、意図したトーンに合わせる。
3. soul を加える（次のセクション参照）。
4. セルフチェック:「これが明らかに AI 生成だと分かる要因は何か」を自問し、残っている tell を修正する。

## Adding soul

パターンを取り除くのは仕事の半分に過ぎない。無機質で声のない文章も同じくらい AI だと分かってしまう。

- **意見を持つ。** 事実に対して中立的に賛否を列挙するのではなく、反応する。
- **リズムを変える。** 短い文。それから、ゆったりと時間をかける長い文。混ぜる。
- **複雑さを認める。** 「impressive」だけより「Impressive but also kind of unsettling」の方がいい。
- **合う場面では「I」を使う。** 一人称は非プロフェッショナルではない。
- **多少の乱れを許す。** 完璧すぎる構造は機械が作ったように見える。
- **具体的にする。** 「this is concerning」ではなく「there's something unsettling about agents churning away at 3am」のように。

## Patterns to detect and fix

### Content

1. **Puffery.** "pivotal moment", "testament to", "evolving landscape", "setting the stage for", "indelible mark", "deeply rooted" のような誇張表現。誇張を削り、実際に起きたことを述べる。
2. **Name-dropping.** 文脈なしにメディア名を並べる。1 つだけ選び、何が言われたかを述べる。
3. **Superficial -ing phrases.** "highlighting...", "ensuring...", "reflecting...", "showcasing...", "fostering..." のような表面的な -ing 句。削除するか、実際の情報源で肉付けする。
4. **Promotional language.** "nestled", "vibrant", "breathtaking", "groundbreaking", "renowned", "stunning", "must-visit" のような宣伝的な言葉。中立的な描写を使う。
5. **Vague attributions.** "Experts believe", "Industry reports suggest", "Some critics argue" のようなあいまいな引用元。情報源を明示するか削除する。
6. **Formulaic challenges.** "Despite challenges... continues to thrive." のような定型的な「困難」表現。具体的な事実に置き換える。

### Language

7. **AI vocabulary.** Additionally, crucial, delve, enduring, enhance, fostering, garner, interplay, intricate, landscape (abstract), pivotal, showcase, tapestry (abstract), testament, underscore, vibrant のような AI 語彙。平易な言葉に置き換える。
8. **Fancy ways to say "is".** "serves as", "stands as", "boasts", "features" のような「is」の凝った言い換え。単に "is" や "has" と言えばよい。
9. **"Not just X, but Y."** この型を使わず、代わりに要点を直接述べる。
10. **Rule of three.** 無理やり 3 つ組にまとめる三の法則。自然な数を使う。
11. **Synonym cycling.** Protagonist、main character、central figure、hero を 1 段落の中で使い分ける類義語の使い回し。1 つを選び、それを繰り返す。
12. **False ranges.** X と Y が意味のある尺度上にないのに "from X to Y" と表現する偽の範囲表現。トピックを直接列挙する。

### Style

13. **Em dash overuse.** em dash は一切使わない。句点かコンマだけを使う（括弧・en dash・ハイフンでの代用も不可）。em dash は AI の tell であり、代わりに括弧を使っても tell を入れ替えるだけである。文を区切りたい場合は、文を終わらせるかコンマを使う。
14. **Colon overuse.** リストや例の前のコロンは問題ない。文中の接続語としては使わない。"If you're coming from traditional automation: instead of registering event handlers, you describe conditions" はコロンがあっても何も加えていない。比較の枠組みを使わずに要点だけが立つように書き直す。"Describing when the scheduler should fire works best as plain English." 意味は同じで、句読点に頼っていない。
15. **Boldface overuse.** 固有名詞や略語をすべて太字にしない。
16. **Inline-header lists.** tell は、太字ラベルとコロンがその行の内容をそのまま繰り返すパターンで、"**Performance:** Performance improved..." のようなもの。これは地の文に変換する。太字の導入部が句点で終わり、項目名を示し、その後に本当に新しい詳細が続く場合（"**Schema in TypeScript.** Tables live in one file."）は問題なく、tell ではない。
17. **Title case headings.** センテンスケースを使う。
18. **Decorative emojis.** 見出しや箇条書きから削除する。
19. **Curly quotes.** ストレートクォートに置き換える。

### Communication artifacts

20. **Chatbot phrases.** "I hope this helps!", "Let me know if...", "Of course!", "Certainly!", "Found the smoking gun!" のようなチャットボット的な言い回し。削除する。
21. **Cutoff disclaimers.** "While specific details are limited..." のような情報不足の言い訳。情報源を見つけるか削除する。
22. **Sycophantic tone.** "Great question! You're absolutely right!" のような追従的なトーン。直接的に応答する。

### Filler

23. **Filler phrases.** "In order to" は "To" にする。"Due to the fact that" は "Because" にする。"It is important to note that" は削除する。
24. **Excessive hedging.** "could potentially possibly be argued that it might" は "may" にする。
25. **Generic conclusions.** "The future looks bright." のような一般論的な結論。具体的な計画や事実を述べる。

### Jargon

26. **Abstract metaphor nouns.** Substrate, wedge, vector, locus, vantage, nexus, primitive（名詞として）, harness（比喩として）, surface（"API surface" のような使い方）, bedrock, scaffolding（比喩として）, modality, paradigm, gold-plating, ratchet（比喩として）, evacuate（コード移動の意味で）, endgame, north star, flywheel のような抽象的な比喩名詞。技術的に見えるが、たいていはもっと平易で具体的な言葉がある。"Substrate" は "base" に。"Wedge in" は "add" に。"Vector" は "way" や "method" に。"Gold-plating" は "more than the job needs" に。"Ratchet" はその仕組みの本当の名前か "a limit that only tightens" に。"Evacuate" は "move out" に。"Endgame" は "the last phase" に。具体的な言葉を選ぶ。

### Plain speech

27. **Say what it does, not how it feels.** "the database stays close at hand"、"SQL you can read"、"types that follow your schema" のような表現は感覚を名指ししているだけである。修正では、仕組みや数値を名指しする。"`.toSQL()` returns the exact string sent to the database"、"a column rename fails the build" のように。その文が読者に何をさせたい／何を知らせたいのかを問い、それを書く。具体的な指示・事実・数値として言い直せないなら削除する。もう 1 つの確認方法として、その文がそのまま別プロジェクトのドキュメントに現れても違和感がないなら、それはこのプロジェクトについて何も語っていない。削除する。
28. **Shorten or split dense sentences.** 読者が文を理解するために読み返す必要があるなら、2 文に分けるか節を削る。1 文に 1 つのアイデア。
29. **Active voice.** 能動態を優先する。"is/are/was/were + 過去分詞" を見つけたら、動作主を明示する。"queries are validated" は "the compiler validates queries" に、"the file is parsed by the loader" は "the loader parses the file" にする。受動態でよいのは、動作主が不明であるか本当に重要でない場合のみ。
30. **Cut adverbs, or use a stronger verb.** "runs quickly" は "is fast" や具体的な数値にする。"significantly improves" は実際に測定した差分にする。弱い動詞を副詞で支えているなら、その動詞が間違っている。
31. **Prefer the plain word.** "utilize" は "use" に、"leverage" は "use" に、"facilitate" は "help" に、"numerous" は "many" に、"in the event that" は "if" にする。凝った類義語が分かりやすいことはめったにない。

日本語の文章に適用する場合も、同じパターン分類で判定する。
