# toshipon-mode-plugin

これは Claude Code 用のプラグインマーケットプレイスリポジトリです。非自明なタスクの標準エントリポイントである `toshipon-mode` skill と、それが呼び出すサブ skill 群、いくつかのコマンド、`architect` agent、`sync-main` スクリプトをひとまとめにしています。

## 由来

このプラグインは、Cursor 向けプラグイン [pstack](https://github.com/cursor/plugins/tree/main/pstack)（Lauren Tan 作、MIT License）を参考に作りました。`toshipon-mode` skill は pstack の `poteto-mode` を、principles や `how`、`why`、`unslop`、`interrogate`、`arena` などのサブ skill は pstack の同名 skill を元にしています。日本語に翻訳したうえで、Claude Code の Agent tool やモデル指定（`opus` / `sonnet` / `haiku`）に合わせて書き換えています。

upstream の変更は定期的に確認して取り込んでいます。最後に取り込んだのは pstack 0.15.43（cursor/plugins `2f87c85`）です。

pstack 以外にも、次のコンポーネントは外部のリポジトリが元になっています。

- `architect` agent と `verification-loop` skill: [ECC（everything-claude-code）](https://github.com/affaan-m/ECC)（MIT License）
- `web-perf` skill: [cloudflare/skills](https://github.com/cloudflare/skills)（Apache License 2.0）
- `japanese-tech-writing` skill: [k16shikano の gist](https://gist.github.com/k16shikano/fd287c3133457c4fd8f5601d34aa817d)（Unlicense）

各ライセンスの著作権表示と本文は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) にまとめています。

## ライセンス

[MIT License](LICENSE)。第三者由来の部分は、それぞれ元のライセンスに従います。

## インストール

Claude Code のセッション内で以下を実行してください。

```
/plugin marketplace add toshipon/toshipon-mode-plugin
/plugin install toshipon-mode@toshipon-tools
```

### microsoft/apm で入れる場合

[APM（Agent Package Manager）](https://github.com/microsoft/apm) を使う場合は、リポジトリ直下の `apm.yml` を経由して、本体と公開されている依存 skill をまとめて入れられます。

```bash
apm install toshipon/toshipon-mode-plugin
```

`apm.yml` は依存だけを宣言するファイルで、本体は `./plugins/toshipon-mode` への依存として解決されます。本体だけが必要な場合は `apm install toshipon/toshipon-mode-plugin/plugins/toshipon-mode` を使ってください。Claude Code の `/plugin` によるインストールは `apm.yml` を読まないため、上の手順には影響しません。

APM は plugin を skill・command・agent に分解して配置するので、`${CLAUDE_PLUGIN_ROOT}` を前提にした参照（`/sync-main` が呼ぶ `scripts/sync-main-branch.sh` など）は動かない可能性があります。Claude Code で使う場合は `/plugin` でのインストールを推奨します。

## 収録コンポーネント

### Skills

- `autonomous-improvement`: 成果物を、仮説・実行・評価・採否の台帳を残しながら反復改善します。採否は決定論的な評価で決め、停滞と完了時には独立監査にかけます。使い方は [専用 README](plugins/toshipon-mode/skills/autonomous-improvement/README.md) を参照してください。
- `harness-evolution`: 実行記録に繰り返し現れる失敗から Skill・指示・評価の改善候補を作り、現行版と同じ条件で比較して、ローカルのレビュー用提案にします。公開とマージは人の承認が要ります。
- `insight-discovery`: 作業に残った摩擦・やり直し・失敗から業務をまたぐ共通構造を探し、反証可能な仮説と最小実験を付けた研究課題の候補を最大 3 件出します。独立監査を通し、新規性は調べた範囲でしか述べません。使い方は [専用 README](plugins/toshipon-mode/skills/insight-discovery/README.md) を参照してください。
- `nfr-design`: 議事録・Confluence・現行 AWS 構成から前提と要件を整理し、矛盾・承認・変更の影響を追跡します。承認済み要件から非機能要件定義書と AWS 方式設計書を生成、検証します。使い方は [専用 README](plugins/toshipon-mode/skills/nfr-design/README.md) を参照してください。
- `toshipon-mode`: 非自明なタスクの標準エントリポイント。playbook を逐語コピーして todolist 化し、principles を適用し、証拠で検証してから完了を宣言します。
- `how`: サブエージェントでコードベースを調査し、サブシステムの動作を解説します。
- `why`: 利用可能なツールを横断的に調査し、コードがそう作られた背景を根拠付きで解明します。
- `unslop`: 文章から AI 特有の癖を取り除きます。
- `technical-writing`: ドキュメント・RFC・PR 説明の構成と文の規律を扱います。
- `show-me-your-work`: 長時間・自律的な作業の判断記録を残します。
- `arena`: 同一タスクに対して複数候補を並列生成し、最良の候補に他の優れた部分を統合します。
- `interrogate`: 複数モデルティアの敵対者がコード変更や設計を壊しにかかり、実行で証明された所見だけを返します。
- `blast-radius`: 変更の影響範囲を評価します。
- `web-perf`: Core Web Vitals などの Web パフォーマンスを分析します。
- `tdd`: テスト駆動開発のガイドラインです。
- `commit-rules`: コミット規約と変更管理のガイドラインです。
- `verification-loop`: PR 前の統合検証（ビルド、型チェック、lint、テスト、セキュリティ）を回します。
- `create-verification-skill`: 実アプリを起動・操作して証拠を残す、リポジトリ固有の検証 skill（`verify-<app>`）を生成します。
- `create-formal-verification-skill`: バグの代償が大きくテストが届きにくいコードを形式検証（TLA+ などの model checker、コード上の verifier）し、変更をその性質に照らして確かめるリポジトリ固有の skill を生成します。
- `maintain-formal-verification-skill`: コードの変更後に形式検証をコードに合わせ直し、各失敗が検証のずれかコードのバグかを判定します。
- `e2e-replay`: iOS シミュレータと cliclick で UI E2E シナリオを再生し、各ステップのスクリーンショットを残します。runner script（`scripts/e2e/run_scenario.sh`）は利用側のリポジトリで用意します。
- `typescript-best-practices`: `.ts` / `.tsx` を読み書きするときの TypeScript の規範です。
- `japanese-writing`: 日本語の文章を Claude が下書きし、Gemini（Antigravity CLI の `agy`）にレビューさせて統合します。同梱の `scripts/gemini-ja.sh` を使います。
- `japanese-tech-writing`: 日本語の技術文書の文章規範（段落の構成、論証の厳密さ、翻訳調の排除など）です。

### Commands

- `/pr-create`: 変更分析に基づく PR 作成を支援します。
- `/pr-feedback`: レビューコメントへの対応と根本原因分析を支援します。
- `/check-github-ci`: GitHub Actions の CI 状況を監視します。
- `/sync-main`: メインブランチの最新を現在のブランチに取り込みます。

### Agent

- `nfr-auditor`: 非機能要件と AWS 方式設計の独立監査 agent です。
- `improvement-auditor`: 改善ループと Harness 改善候補を、別のコンテキストで監査する agent です。読むだけで、合格条件も成果物も変更しません。
- `harness-evolution`: 実行記録の失敗パターンから改善仮説を立て、隔離 worktree に候補版を作る agent です。
- `insight-discovery`: 記録された摩擦から観測・共通構造・仮説・最小実験の草案を作る agent です。ツールを持たず、採否や順位は決めません。
- `insight-auditor`: 研究課題の候補を、既存手法で足りないか・問題設定が誤っていないか・反証可能かの 3 点で監査する agent です。
- `architect`: システムアーキテクト agent です。実装前に並列で設計を探索する用途に使います。

### Scripts

- `sync-main-branch.sh`: `/sync-main` コマンドが呼び出す、メインブランチ同期の実処理です。

## 外部参照している skill（未収録）

このプラグインの skill は、以下の skill を名前で参照しますが、このプラグインには含まれていません。`/plugin` でインストールした場合は、利用側の環境に別途インストールされている前提です。

APM でインストールした場合は、公開されている取得元があるものだけ `apm.yml` の依存として一緒に入ります。

| skill | `apm.yml` での扱い |
|---|---|
| `hdd:hypothesis-first`、`hdd:verify`、`hdd:grilling`、`hdd:source-traceability`（`japanese-writing` が参照） | 依存として宣言済み（[toshipon/hypothesis-driven-development-skills](https://github.com/toshipon/hypothesis-driven-development-skills) の `plugin/`） |
| `superset:orchestrate`、`superset:browser` | 未宣言。Superset アプリが提供する skill のため |
| `claude-in-chrome` | 未宣言。Claude Code に組み込まれている skill のため |

`unslop`、`create-verification-skill`、`e2e-replay` はこのプラグインに収録済みです。

## 注意: skill の二重ロードについて

このプラグインが収録している skill と同名の skill が、すでに `~/.claude/skills/` 配下にインストールされているマシンでは、両方が二重にロードされます。該当する skill 名（`autonomous-improvement`、`harness-evolution`、`insight-discovery`、`nfr-design`、`toshipon-mode`、`how`、`why`、`unslop`、`technical-writing`、`show-me-your-work`、`arena`、`interrogate`、`blast-radius`、`web-perf`、`tdd`、`commit-rules`、`verification-loop`、`create-verification-skill`、`create-formal-verification-skill`、`maintain-formal-verification-skill`、`e2e-replay`、`typescript-best-practices`、`japanese-writing`、`japanese-tech-writing`）が個人設定側にもある場合は、どちらか片方を削除してください。

## 成果物と Harness の継続改善

`/toshipon-mode:autonomous-improvement` は、目的・制約・ベースライン・合格条件・予算を最初に固定し、仮説の登録、実験、測定、採否を追記専用の台帳に残しながら成果物を改善します。回帰した実験は自分の変更だけを戻し、利用者の未コミット変更には触れません。同じ失敗が 3 回、または採用の無い試行が 3 回続くと独立監査を求め、未達を合格として終了できません。

`/toshipon-mode:harness-evolution` は、その実行記録から繰り返す失敗を拾い、Skill や指示の候補版を隔離 worktree に作って、現行版と train / holdout のケースで比較します。事前に決めた改善条件と独立監査を通った候補だけが、差分・測定結果・リスク・ロールバック手順を含むローカルの提案になります。評価 suite の変更、提案の承認、公開は人が記録します。

実行記録は repository の外（既定 `~/.local/state/ai-harness/`）に置きます。Claude Code の hook と launchd による自動起動は設定例と手順だけを同梱し、既定では何も有効になりません。Python 3.10 以上と `jsonschema` が必要です。手順、設定、停止方法、制限は [autonomous-improvement README](plugins/toshipon-mode/skills/autonomous-improvement/README.md) にあります。

## 研究課題の発見

`/toshipon-mode:insight-discovery` は、成果物でも作業手法でもなく、問題設定を扱います。作業メモ、土台の実行記録、レビュー指摘、Claude Code `/insights` が残した構造化項目を証拠として取り込み、異なる業務に共通する形を探して、前提を疑う仮説を作ります。モデルが返すのは各段の JSON だけで、証拠 ID の実在、引用が原文にあること、複数の業務にまたがること、反証条件があることを script が検査します。独立監査が「既存の手法で足りる」とした候補は autonomous-improvement / harness-evolution 宛てのメモとして引き渡し、研究課題だけを最大 3 件残します。

週次では、利用者が自分の仮説を先に書いてから生成された仮説を読み、実験を 1 件だけ選びます。既存手法の外部検索は、対話で実行ごとに承認したときだけ行い、調べていない候補は「未調査」と表示します。日次・週次の実行は autonomous-improvement の scheduler に載せられますが、既定では何も自動起動しません。手順、入力の形式、制限は [insight-discovery README](plugins/toshipon-mode/skills/insight-discovery/README.md) にあります。

## 非機能要件と AWS 方式設計

`/toshipon-mode:nfr-design <workspace>` で、議事録・Confluence・現行構成を版と引用付きで取り込み、既存の Doc workflow に非機能設計の検証を追加できます。出典の食い違いは自動で決着させず、人が確認した要件だけを設計へ渡します。`diff` は出典変更による要件・設計・検証の見直し候補を出します。Python 3.10 以上と `jsonschema` が必要です。専用 venv を利用側に用意し、plugin cache は変更しません。認証不要のデモ、Claude Code の API key / subscription login による生成、独立監査、既定 3 回の反復に対応します。

実行手順、データ契約、テストは [nfr-design README](plugins/toshipon-mode/skills/nfr-design/README.md) にあります。生成物は新しい run ディレクトリに保存し、既存の台帳や文書を上書きしません。文書品質の通過は業務承認や AWS 上の実測達成とは別です。既存 `loop-engineering` のマージ・デプロイループは起動しません。

この Skill の launcher は plugin の同梱パスを使います。まず Claude Code の `/plugin` 配布を対象とし、APM が runtime・Schema・Agent を同じ配置で保持しない構成は未検証です。
