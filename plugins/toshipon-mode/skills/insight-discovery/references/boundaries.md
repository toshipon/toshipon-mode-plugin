# 隣の skill との境界

| skill | 扱うもの | Insight Discovery との関係 |
|---|---|---|
| `autonomous-improvement` | 成果物を、測定できる合格条件に向けて改善する | 監査で「成果物の改善で足りる」とされた候補を、メモとして引き渡す |
| `harness-evolution` | 繰り返す失敗から Skill・指示・評価の候補版を作り、比較する | 監査で「作業手法の改善で足りる」とされた候補を、メモとして引き渡す |
| Claude Code `/insights` | 使い方の集計と改善提案 | 残された構造化項目を入力として読む。`/insights` 自体は起動しない |
| `loop-engineering` | 実装・マージ・デプロイ・計測まで閉じるプロダクトのループ | 重ならない。Insight Discovery はマージもデプロイもしない |
| hdd の各 skill（別 plugin） | プロダクトの仮説の鋭利化と検証手法 | accepted にした候補を人が持ち込める。依存はしない |

## 引き渡し

引き渡しは `<state home>/insight/handoffs/<候補 ID>.json` と、台帳の `handoff_recorded` である。メモには、抽象化した問題、仮説、反証条件、証拠 ID、理由が入る。評価基準や合格条件は入らない。

受け取る側への登録は人が行う。

- Improvement に渡す場合: メモを読み、対象の run に `harness.py hypothesis add <run> --file` で仮説を登録する。`rationale` と `family` は登録する人が書く。
- Evolution に渡す場合: `harness.py evolve analyze` が同じ失敗を `eligible` に挙げているかを確かめ、挙がっていれば `evolve hypothesis-add --file` で登録する。挙がっていなければ、Evolution の発動条件（件数と出所の数）に達していない。

Insight Discovery のコードは、土台の評価、比較、評価 suite と提案の承認の関数を呼ばない。土台から使うのは、人の承認を記録する共通のゲート（`require_human`）だけである。呼ばないことは、ソースを走査するテスト（`tests/test_cli.py` の `SourceBoundaries`）で確かめている。
