---
name: why
description: git 履歴・issue トラッカー（Linear / Jira）・長文ドキュメント（Notion / Confluence）・チャット・観測基盤・エラートラッカー・分析ウェアハウス（Databricks / BigQuery）を、利用可能な MCP ツールと gh CLI で並列に調査し、コードがその形になった背景と意図を出典付き・確信度付きで解明する。「なぜこう実装されている？」「この定数はどこから来た？」「この分岐は何のためにある？」と聞かれたときに使う
---

# Why

コードの背後にある動機と意図を調査する。相棒の `how` スキルはコードが何をし、どう動くかに答える。`why` はどんな力がその形を生んだかに答える。

起動はすべて Agent tool の `subagent_type: "general-purpose"` で行う。**読み取り専用の Explore は使わない。** MCP ツールにアクセスできず、MCP を使う investigator と、統合者による引用のスポットチェックが働かなくなるためである。investigator はそれでもリポジトリに何も書き込まない。各サブエージェントの `effort` は toshipon-mode skill の Delegation 節に従って仕事に合わせて選ぶ。

## Operating Posture

**慎重で、注意深く、精密な調査官**として振る舞う。知っていることと推測していることを正直に区別する。確信度フレームワークとフレージングガイドの全体は `references/epistemics.md` を読むこと。統合者はこれに従わなければならない。

## Step 1. 対象と質問を理解する

**対象**は通常、コードの塊、パターン、機能、あるいは名指しされた設計判断である。**質問**は通常、設計の根拠、トレードオフ、動機となったエッジケース、外部の制約、デッドコード、あるいは広範な歴史の調査である。

対象が曖昧な場合(明確な指示対象のない「なぜこのやり方をしているのか?」)は、会話の文脈(開かれているファイル、直近の編集、カーソル位置、直前に議論されたこと)から最有力の推測を行う。自分の解釈を簡潔に述べ、的外れならユーザーが軌道修正できるようにしたうえで進める。

## Step 2. コードのアンカーを確立する

investigator を起動する前に、シード文脈をその場で組み立てる。ファイルパスと行範囲、主要なシンボル(関数、クラス、定数)、対象に触れた直近の数コミット、マージコミットからの PR 番号(subject line 内の `(#1234)` というパターン)、紐づくチケット ID である。最終変更コミットには `git blame -L <start>,<end> <file>`、リネームを追ったパッチ付きの全履歴には `git log --follow -p -- <file>`、PR 番号が見える直近のコミットには `git log --oneline -20 -- <file>`、PR 番号の抽出には `git log -1 --format=%B <commit>` を使う。そのうえで、実質的な変更を伴うすべてのコミットについて PR の本文と議論を取得する: `gh pr view <number> --json title,body,author,createdAt,mergedAt,labels,closingIssuesReferences,comments,reviews`。このアンカーはすべての investigator と統合者に渡す。

## Step 3. investigator を並列に起動する

**完全な並列調査を既定とする。** 目標は最小限ではなく完全なカバレッジマップである。検索をスキップせず、null を記録する。

### Discovery

investigator を起動する前に、現在の環境で利用可能な MCP ツールと `gh` CLI を列挙し、それぞれを次の 7 つの証拠カテゴリのいずれかにマッピングする。

1. ソースコード管理履歴
2. issue / チケットトラッカー
3. 長文ドキュメント
4. リアルタイムのチームチャット
5. インフラ観測基盤
6. エラー / 例外トラッキング
7. プロダクト分析ウェアハウス

専用のエラートラッカーが無く、観測基盤（Datadog の Error Tracking など）がエラーを集約している場合は、6 を 5 の investigator に統合し、カバレッジマップに「6 は 5 に統合」と書く。環境に無いカテゴリはギャップとして記録する。

ソースコード管理は git と `gh` を通じて常に利用可能である。他の 6 つについては、MCP 名、サーバーの指示、ツール名、リソース記述子を使って分類する。ある MCP が複数のカテゴリに当てはまりそうな場合は、その主要な証拠に合う方を選ぶ。曖昧なケースはカバレッジマップに記録する。

一致する MCP を持つカテゴリごとに investigator を 1 人、すべてを 1 つのメッセージで起動して並行に走らせ、各自に正確に 1 つのツールまたは MCP を担当させる。1 つのエージェントに複数の MCP をまたがせないこと。`model` は `"sonnet"` を既定とし、掘り下げが必要な調査では `"opus"` に格上げしてよい。

各 investigator のプロンプトは `references/investigator-prompt.md` を埋めたものに、以下を加える:
- 選ばれた MCP に対応する、`references/source-playbook.md` の例から適応させたカテゴリ別プレイブック `references/sources/<source>.md`
- **対象コードが防御的に見える場合**(null チェック、リトライロジック、タイムアウト処理、レートリミッティング、フィーチャーフラグ、egress ガード、OOM ハンドラ)は `references/sources/incident-postmortem.md`
- コードアンカーとユーザーの元の質問

### Investigator roster。利用可能な証拠カテゴリごとに 1 人

下記の各カテゴリは、「Best at」の後に示す形の「なぜ」を独自に明るみに出せる位置にある。何が返ってくるかを予想し、カテゴリが空で返ったときにギャップに名前をつけ、まれな証拠のある無関係ケースに限りスキップを正当化するために使う。

1. **ソースコード管理**(git 履歴、PR 用の `gh`、コードコメント、テスト)。常に起動する。唯一保証された情報源である。Best at: *レビュー時に記録された実装時の根拠*。
2. **issue / チケットトラッカー**(例: Linear、Jira、GitHub Issues、Plane、Shortcut の MCP)。Best at: *プロダクトあるいはビジネス上の強制要因*。理由がエンジニアリングの外部にある場合に最も強い。
3. **長文ドキュメント**(例: Notion、Confluence、Google Docs、Coda の MCP)。Best at: コードになる前に書き出された*長文の設計根拠*。
4. **リアルタイムのチームチャット**(例: Slack、Discord、Microsoft Teams、Mattermost の MCP)。Best at: *ドキュメントに残らなかったリアルタイムの議論*。ソースコード管理、チケット、ドキュメントの記録が薄いときに特に重要。
5. **インフラ観測基盤**(例: Datadog、New Relic、Honeycomb、Grafana、Splunk の MCP)。Best at: *コードを動機づけたインフラとランタイムの現実*。対象がインフラのシグナル(タイムアウト、リトライ、レートリミット、サーキットブレーカー)に反応している場合に最も強い。
6. **エラー / 例外トラッキング**(例: Sentry、Rollbar、Bugsnag、Airbrake の MCP)。Best at: *防御的あるいは修正的なコードを動機づけた具体的な例外とエラーの推移*。catch ブロック、null ガード、型チェック、リトライ、その他の防御に対して最も強い。
7. **プロダクト分析ウェアハウス**(例: Databricks、Snowflake、BigQuery、ClickHouse、dbt、Redshift の MCP)。Best at: *コードを形作ったプロダクトとデータの現実*。フラグで制御されたコード、実験駆動の出荷、データマイグレーション、「この数値はどこから来たのか」という質問に最も強い。

### いつ investigator をスキップするか

最終的な Sources Consulted に記載する**明示的で書面化された正当化**がある場合のみスキップする。妥当な理由は 2 つ:

- この環境で**そのカテゴリに一致する MCP がない**。選択ではなくギャップとしてフラグを立てる: 「Real-time team chat skipped. No matching MCP available, so the conversational record was not searchable.」
- その情報源が**証拠のある形で無関係である**。「おそらく無関係」ではない。高い基準である: 「Error / exception tracking skipped. Target is a build-time script with no runtime code path.」

スコープ評価の結果、PR の説明にすでに完全な答えが含まれている単一コミットの些細な対象であることが示唆される場合に限り、7 つすべての利用可能なカテゴリ検索が冗長であることを確認した**後にのみ**インラインで答えてもよい。その旨を明示的に述べること。これはまれであるべきである。

## Step 4. 統合する

統合者を 1 人起動する。`model` は `"opus"`。統合者には以下を渡す:
1. investigator の調査結果。null 結果と、正当化付きでスキップされたカテゴリを含む
2. コードアンカー
3. ユーザーの元の質問
4. `references/epistemics.md` の確信度フレームワーク
5. `references/synthesizer-prompt.md` の統合者プロンプトテンプレート

## Step 5. 提示する

統合者の出力をユーザーに提示する。明確さのために軽く編集したり会話からの文脈を加えたりしてもよいが、**確信度の言い回しは書き直さない**。

## Output Format

構造は `references/synthesizer-prompt.md` のものを使う: The Question、The Code in Question、What We Found、What We Can Reasonably Infer、Competing Hypotheses、What We Don't Know、Sources Consulted、Confidence Summary。必要に応じて調整するが、確信度の分離は維持し、Sources Consulted は investigator ごとに 1 行とし、何も返さなかったものやスキップしたものも理由付きで含める。

Sources Consulted の後、ユーザーの `why` 質問がこのコードを変更する前段階のものであれば、系譜の調査結果を、その変更を計画するための Preserve / Change / Avoid / Risk の制約セットに変換する。

## Common Failure Modes to Avoid

- **直近バイアス。** 最新のコミットが権威的だと決めつけること。現在の形は、しばしば多くの以前の決定の積み重ねである。さかのぼってたどること。
