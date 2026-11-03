# 検証記録

2026-11-03 にローカルで確認しました。認証情報、実案件の入力、Claude の会話記録は含めていません。

| 検査 | 結果 |
|---|---|
| Python 3.10.20 | 26 テスト成功、構文確認成功 |
| Python 3.15.2 | 26 テスト成功 |
| coverage 7.17.1 | runtime/harness 全体 92% |
| pyright 1.1.414 | errors 0、warnings 0 |
| ruff 0.17.3 | lint と format 成功 |
| 既存 doc-check | デモ・既存候補評価の両方でエラー 0 |
| 既存 loop-selftest | 全検査成功 |
| Claude Code 2.1.317 | plugin manifest と marketplace manifest が検証成功 |
| CLI の実接続 | subscription、safe-mode、ツール無効で構造化応答を取得 |
| 実生成・独立監査 | 設計 JSON と監査 JSON を取得し、品質 gate が不合格として保存 |

実接続の検査には同梱の合成サンプルを使いました。生成者は 8 件の未決事項、監査者は 17 件の指摘を返しました。最終結果は critical 1、high 15、medium 8、low 1 で、API エラーはありませんでした。根拠不足、未承認の入力、AZ 障害時の容量計算などを重大指摘として返し、回数上限で終了しました。サンプルを本番設計として合格にした結果ではありません。

実接続の最初の試行で Claude CLI が Draft 2020-12 の宣言を拒否することを確認しました。CLI 転送時のみトップレベルの $schema を除く修正後、生成と監査が完了しました。ローカルの Draft 2020-12 検証は維持しています。

plugin validator は既存 plugin に icon がないという配布用の警告を 1 件表示します。manifest の検証自体は成功しています。この変更で icon は追加していません。

認証不要のテストは初回失敗から修正版の合格までを確認しています。実 Claude の検査は 1 反復で止め、合成入力を正しく不合格にできることを確認しました。API key 認証、APM 配置、本番 AWS 上の負荷試験・復旧訓練は未実施です。

CI は Python 3.10 / 3.13 で同じ自動テスト、型・lint・coverage、既存 loop-selftest を実行します。Claude への有料リクエストや認証情報は CI に必要ありません。
