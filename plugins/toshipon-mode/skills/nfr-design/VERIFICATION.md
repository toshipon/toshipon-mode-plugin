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

CI は Linux の Python 3.10 / 3.13 で同じ自動テスト、型・lint・coverage を実行します。既存 loop-selftest は冒頭の規約どおり macOS の /bin/bash 3.2 を対象とするため、macOS 専用ジョブで検証します。最初の CI では、この既存テストを Linux で実行して通知関連の 5 検査が失敗したため、実行環境を分けました。Claude への有料リクエストや認証情報は CI に必要ありません。


## 文脈取り込みと変更追跡の追加検証

同日に context 機能を追加し、Python 3.10.20 と 3.15.2 で 54 テストを検証しました。元の Harness テスト 26 件を維持し、引用・hash・承認・食い違い・adapter・変更影響の 28 件を追加しています。runtime 全体のカバレッジは約 89% です。

- file / Confluence Cloud / AWS CLI の adapter を検証しました。Confluence の tenant、ページ ID、redirect、認証情報を残さないこと、AWS の固定 read-only command と順序差の正規化は mock を使っています。
- 実 Confluence / AWS の認証を伴う接続は行っていません。CLI の subscription で実 Claude による原文抽出を実行し、6 件（要件 2、現状 2、提案 1、不明 1）を引用付きで保存しました。全件が未承認で、assess は RTO の食い違いを 1 件検出し、合格にしませんでした。
- 同梱の合成サンプルでは collect → extract --proposals → assess の不合格 → DEMO の明示的 review → prepare → demo の合格を、実際の CLI 呼び出しで確認しました。
- サンプルの議事録を 30 分から 15 分へ変更して再収集し、diff が REQ-001 / D1 / V1 と C1 の承認再確認を出すことを確認しました。
- 出典本文の改変、同じ本文の版変更、置換した旧資料の変更、承認後の抽出値の改変、生成中の変更、unknown な測定期間、異なる範囲の誤比較を検証しています。
- 文脈がある入力は、context_binding や context_refs を取り除いて gate を省略できません。原文と抽出内容は独立監査にも渡します。
- 出典変更の影響は保守的な見直し候補です。直接参照と潜在影響を区別し、構成変更の実害や影響範囲を証明した結果とは扱いません。

本人確認、原文の真偽判定、全資料の網羅性、自然言語の解釈の完全な正しさは保証しません。人が判断を確認し、業務側の正式な承認証跡と結び付ける前提です。
