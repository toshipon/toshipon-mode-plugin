# Performance Optimization

## Model Selection Strategy

Agent tool の `model` オプションには**エイリアス**（`haiku` / `sonnet` / `opus` / `fable`）を指定する。
エイリアスは各ティアの最新モデルに解決されるため、バージョン付き ID や価格はここに書かない
（現行モデルと価格は `claude-api` skill を参照）。

選ぶ基準はスコープが切れているかどうか。切れているなら `sonnet`、切れていないなら `opus`。

- **`haiku`**: 軽量・高頻度に呼び出す agent、分類・抽出などの単純タスク、multi-agent 構成の worker
- **`sonnet`**: スコープの切れた実装（バグ修正、既存機能の反復）、定型化した agent タスク、ドキュメントやスライドの整形、multi-agent workflow のオーケストレーション
- **`opus`**: 判断を要する複雑な作業、設計判断、長時間の自律実行、深い推論を要する調査
- **`fable`**: opus で足りないと分かった場合のみ明示指定する

## Effort

- agentic な作業は `medium` 起点。スコープが曖昧なもの、難しいものだけ `high` に上げる
- 対話や低遅延が要る用途は `medium` か `low`
- `xhigh` / `max` は eval で品質改善を示せた場合だけ使う
- 思考量を減らしたい時は effort を下げる。system prompt で「考えすぎるな」と書いても thinking は確実には減らない
- Claude Code では thinking を無効化できない。調整できるのは effort（`settings.json` の `effortLevel`。現在は `high`）とスコープの切り方だけ

Claude API のパラメータ（effort、thinking、sampling）を扱うコードを書くときは `claude-api` skill に従う。

## Context Window Management

harness が自動で compaction するため、context の残量を理由に作業を止めない。
大規模リファクタリングや複数ファイルの機能実装では、フェーズの区切り（調査→実装、機能完了）で
`/compact` を検討する。

## Build Troubleshooting

ビルドが失敗したら build-error-resolver agent に渡す。
