# toshipon-mode-plugin

これは Claude Code 用のプラグインマーケットプレイスリポジトリです。非自明なタスクの標準エントリポイントである `toshipon-mode` skill と、それが呼び出すサブ skill 群、いくつかのコマンド、`architect` agent、`sync-main` スクリプトをひとまとめにしています。

## 由来

このプラグインは、Cursor 向けプラグイン [pstack](https://github.com/cursor/plugins/tree/main/pstack)（Lauren Tan 作、MIT License）を参考に作りました。`toshipon-mode` skill は pstack の `poteto-mode` を、principles や `how`、`why`、`unslop`、`interrogate`、`arena` などのサブ skill は pstack の同名 skill を元にしています。日本語に翻訳したうえで、Claude Code の Agent tool やモデル指定（`opus` / `sonnet` / `haiku`）に合わせて書き換えています。pstack の著作権表示は `Copyright (c) 2026 Lauren Tan` です。

upstream の変更は定期的に確認して取り込んでいます。最後に取り込んだのは pstack 0.15.5（cursor/plugins `12d587d`）です。

## インストール

Claude Code のセッション内で以下を実行してください。

```
/plugin marketplace add toshipon/toshipon-mode-plugin
/plugin install toshipon-mode@toshipon-tools
```

このリポジトリは private のため、`gh auth login` や SSH 鍵などの git 認証がローカルに設定されている必要があります。バックグラウンドでの自動更新チェックには、SSH 経由のアクセスか `gh auth setup-git` による HTTPS 認証情報の設定のどちらかが要ります。認証が無い環境では marketplace の追加やインストール自体が失敗します。

## 収録コンポーネント

### Skills

- `toshipon-mode`: 非自明なタスクの標準エントリポイント。playbook を逐語コピーして todolist 化し、principles を適用し、証拠で検証してから完了を宣言します。
- `how`: サブエージェントを並列起動してコードベースを調査し、サブシステムの動作を解説します。
- `why`: 利用可能なツールを横断的に調査し、コードがそう作られた背景を根拠付きで解明します。
- `unslop`: 文章から AI 特有の癖を取り除き、人間らしい声を加えます。
- `technical-writing`: ドキュメント・RFC・PR 説明の構成と文の規律を扱います。
- `show-me-your-work`: 長時間・自律的な作業の判断記録を残します。
- `arena`: 同一タスクに対して複数候補を並列生成し、最良の候補に他の優れた部分を統合します。
- `interrogate`: 複数モデルティアでコード変更を敵対的にレビューします。
- `blast-radius`: 変更の影響範囲を評価します。
- `web-perf`: Core Web Vitals などの Web パフォーマンスを分析します。
- `tdd`: テスト駆動開発のガイドラインです。
- `commit-rules`: コミット規約と変更管理のガイドラインです。
- `verification-loop`: PR 前の統合検証（ビルド、型チェック、lint、テスト、セキュリティ）を回します。

### Commands

- `/pr-create`: 変更分析に基づく PR 作成を支援します。
- `/pr-feedback`: レビューコメントへの対応と根本原因分析を支援します。
- `/check-github-ci`: GitHub Actions の CI 状況を監視します。
- `/sync-main`: メインブランチの最新を現在のブランチに取り込みます。

### Agent

- `architect`: システムアーキテクト agent です。実装前に並列で設計を探索する用途に使います。

### Scripts

- `sync-main-branch.sh`: `/sync-main` コマンドが呼び出す、メインブランチ同期の実処理です。

## 外部参照している skill（未収録）

`toshipon-mode` の SKILL.md は、以下の skill を名前で参照しますが、このプラグインには含まれていません。利用側の環境に別途インストールされている前提です。

- `hdd:hypothesis-first`
- `hdd:verify`
- `hdd:grill`
- `superset:orchestrate`
- `superset:browser`
- `claude-in-chrome`
- `e2e-replay`
- `create-verification-skill`

`unslop` はこのプラグインに収録済みです。

## 注意: skill の二重ロードについて

このプラグインが収録している skill と同名の skill が、すでに `~/.claude/skills/` 配下にインストールされているマシンでは、両方が二重にロードされます。該当する skill 名（`toshipon-mode`、`how`、`why`、`unslop`、`technical-writing`、`show-me-your-work`、`arena`、`interrogate`、`blast-radius`、`web-perf`、`tdd`、`commit-rules`、`verification-loop`）が個人設定側にもある場合は、どちらか片方を削除してください。
