# Doc workspace

要求定義・設計書などの成果物ドキュメントを、Confluence の記事、Slack の議論、規制文書（FISC、US-SOX、SOC1 など）、既存システムから起こす作業のための台帳と規約。Doc discovery、Doc question loop、Doc author の 3 つの playbook が共有する。

ドキュメント作業の証拠は出典である。**出典のない事実は事実ではなく質問である。** 成果物に書くすべての記述は、台帳のどれかを指す札を持つ。台帳が真実であり、ドキュメントは台帳の view である。

## Where the workspace lives

置き場所は skill を走らせている環境から決める。順に確認し、最初に当てはまったものを採用する。

1. リポジトリ内で、既にドキュメントの置き場所の規約がある（`docs/`、`doc/`、`design/`、ADR のディレクトリ、Confluence 転記用のディレクトリなど）。その規約に従い、その下に `<slug>/` を切る。
2. リポジトリ内で規約がない。`docs/<slug>/` を作る。
3. リポジトリではない。cwd 直下に `<slug>/` を作る。

`<slug>` は対象システムや案件を表す短い kebab-case。決めたパスと理由を `decisions.tsv` の最初の行に記録する。次のセッションは `find . -name questions.tsv` で workspace を見つける。

`sources/` は既定で gitignore する。Confluence の本文や Slack のスレッドは機密であり、index である `sources.tsv` だけを git に残す。gitignore できない環境では `sources/` を作らず、`sources.tsv` の locator だけで運用する。

## Layout

```
<workspace>/
  sources.tsv        集めた材料の索引。S1.. の ID
  sources/           材料の本文。gitignore 既定
  facts.md           わかっていること。1 事実 1 行、末尾に出典札
  questions.tsv      質問票。Q1.. の ID
  decisions.tsv      作業中の判断の記録。show-me-your-work の形式
  requirements.md    要求定義書（成果物）
  design.md          設計書（成果物）
```

成果物の skeleton は `skeletons/requirements.md` と `skeletons/design.md` にある。他の種類（運用手順、監査対応資料）が要る場合は Doc author の step 1 に従って skeleton を先に設計する。

## sources.tsv

列は `id	kind	locator	title	retrieved	note`。

- **id。** `S1`、`S2`、と振る。成果物からは `[S3]` の形で参照する。
- **kind。** `confluence`、`slack`、`regulation`、`code`、`meeting`、`interview`、`other` のどれか。
- **locator。** Confluence の URL、Slack の permalink、規制文書のファイルパスと版、コードなら `path:line` かディレクトリ。
- **retrieved。** 取り込んだ日。Confluence も Slack も後から書き換わるので、いつ時点の内容かを残す。
- **note。** 一言。何について書かれた材料か。

本文は `sources/S3-<slug>.md` に保存する。長い材料は main thread に貼らず、subagent に読ませて `facts.md` の候補行だけを受け取る（[guard-the-context-window](principles/guard-the-context-window.md)）。

## facts.md

話題ごとの見出しの下に、1 事実 1 行で書く。各行は `F<n>.` で始まり、末尾に出典札を持つ。

```markdown
## 既存の承認フロー

- F12. 出金は 2 名承認で、承認者は `approvers` テーブルの role が `manager` の利用者に限られる [S3][S7]
- F13. 承認ログは 7 年保持している。保持先は S3 バケット `audit-logs` [S7]
```

- 出典のない行は書かない。書きたくなったら、それは `questions.tsv` の行である。
- 材料同士が矛盾する場合は両方の行を残し、矛盾を解く質問を `questions.tsv` に起こす。
- `facts.md` は追記のみ。誤りが分かったら行を消さず、訂正行を足して元の行に `(F20 で訂正)` と付ける。

## questions.tsv

列は `id	question	blocks	owner	status	asked_at	answer	evidence	incorporated_in	updated`。`scripts/question.sh` で操作する。手で編集しない。

- **blocks。** この質問が阻んでいるもの。skeleton の節名か `REQ-` の ID。空にしない。阻んでいるものが言えない質問は、今聞く必要がない。
- **owner。** 誰が答えられるか。人名か役割。空なら `(未定)` として扱われ、owner を決めること自体が最初の作業になる。
- **status** は state machine である。
  - `open`。起こしたばかり。まだ誰にも聞いていない。
  - `asked`。owner に送った。`asked_at` が入る。
  - `answered`。回答を得た。`answer` に内容、`evidence` に回答を取り込んだ `S<n>`。
  - `assumed`。回答が来ないので推奨回答で仮置きして進めた。`answer` に仮の内容。成果物では `[Q7 仮]` として見える。確認が取れれば `answered` へ、覆れば `open` に戻す。
  - `incorporated`。成果物に反映した。`incorporated_in` に節名。
  - `dropped`。聞く必要がなくなった。理由を `answer` に書く。

```
open ──▶ asked ──▶ answered ──▶ incorporated
  │                   ▲
  └──▶ assumed ───────┘ (確認された)
         │
         └──▶ open (覆った)
どの状態からも dropped へ
```

### 質問票に載せる前に分類する

未知に気づいたら、質問票に載せる前に 3 つに分類する。SKILL.md の「質問する前に分類する」と同じ規律である。

- **読めば分かる。** 材料がどこかにある。読みに行き、`sources.tsv` に足し、`facts.md` に書く。質問票には載せない。
- **試せば分かる。** 挙動やタイミングや性能の問い。`playbooks/prototype.md` で決着させる。
- **人が決める。** プロダクトの方針、優先度、責任分界、監査上の解釈。これだけが質問票に載る。

質問には推奨回答を添える。owner は判断ではなく反応でよくなり、回答が早くなる。

## decisions.tsv

`skills/show-me-your-work/` の形式と `scripts/log.sh` をそのまま使う。置き場所の決定、skeleton の選択、仮置きの判断、review で覆した点を 1 行ずつ残す。ドキュメント作業は日をまたぐので、この記録が次のセッションの resume point になる。

## Evidence tags in deliverables

成果物のすべての記述は、次のどれかの札を末尾に持つ。

- `[S<n>]`。`sources.tsv` の材料に基づく事実。
- `[Q<n>]`。`questions.tsv` の質問に基づく。`answered` か `incorporated` なら回答に基づく記述、`open` か `asked` ならその記述は未決であり、同じファイルの `## 未決事項` に載っていなければならない。
- `[Q<n> 仮]`。`assumed` の仮置き。読者に仮であることを見せる。
- `[D<n>]`。設計書の「決定と代替案」の表の行。決定の行自体は `[S]` か `[Q]` を持つ。

要求は `- REQ-001. <要求> [S3]` の形で書く。ID は 3 桁ゼロ埋め、番号は振り直さない。設計書は各節が対応する `REQ-` を名指しする。`scripts/doc-check.sh <workspace>` が、札のない要求、台帳にない `S`/`Q`、未決事項に載っていない open な `Q`、要求定義書と設計書の間の `REQ-` の対応を検査する。

## Regulatory requirements

FISC 安全対策基準、US-SOX の ITGC、SOC1 の統制目標などの条項番号や統制 ID を、記憶から書くことを禁止する。版が変われば番号が変わり、監査人は番号で照合する。

- 引用できるのは `kind=regulation` の材料からだけである。基準の本文、社内の統制マトリクス、監査法人からの要求一覧を `sources.tsv` に取り込んでから、該当項目を条項 ID 付きで `facts.md` の「外部要件」に抜粋する。
- ユーザーが基準名を挙げたが材料がない場合、それは質問である。「対象となる基準の版と、適用される項目の範囲」を owner 付きで `questions.tsv` に起こす。
- 成果物の外部要件は表で持つ。基準、項目 ID、要求内容、出典、対応する `REQ-` の列。設計書の統制設計は、統制ごとに `REQ-`、実装箇所、監査時に出せる証跡を名指しする。

## Source adapters

材料の取り込み経路は実行時に判定する。上から順に試し、使えたものを使う。

1. **MCP。** `ToolSearch` で `confluence`、`atlassian`、`slack` を検索する。tool があればそれで本文を取得し、`sources/` に保存する。
2. **ブラウザ。** `claude-in-chrome` でログイン済みのページを開き、`get_page_text` で本文を取り出して保存する。Slack はスレッドの permalink を開く。
3. **ユーザーが貼る、置く。** どちらも使えない場合、必要な材料を「素材依頼」として reply に列挙する。ページ名と何を知りたいかを添える。これは質問票の行ではない。質問票は人が決めることのためにある。

どの経路でも、`sources.tsv` の locator には元の URL や permalink を残す。保存した本文ではなく元の場所が出典である。

## Output formats

成果物は Markdown で書く。2 つの行き先がある。

- **git 管理する。** リポジトリの規約の場所に置き、PR で出す。`sources/` は含めない。
- **Confluence に転記する。** 同じ Markdown を貼る。表は pipe table で書く。mermaid の図は転記先で描画されるかを先に確認し、描画されない環境では図を PNG にするか、表に落とす。質問票は `scripts/question.sh <tsv> md` で owner ごとの表に変換して貼る。
