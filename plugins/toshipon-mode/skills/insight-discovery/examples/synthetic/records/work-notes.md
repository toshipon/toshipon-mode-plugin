# 合成の作業メモ（架空の 3 業務。実在の業務・顧客・システムとは関係ありません）

classification: synthetic

## billing-reconciliation | back-office
- [retry:rerun-after-late-check] 月次の請求突合バッチを最後まで流してから、入力 CSV に通貨コード列が欠けていると分かり、バッチ全体を再実行した
- [retry:rerun-after-late-check] 突合結果の件数が合わず調べたところ、取り込み元の締め日が 1 日ずれていた。取り込みからやり直した
- [constraint] 入力 CSV の形式は取り込み側では検査していない

## ci-flaky-tests | software-delivery
- [retry:rerun-after-late-check] 統合テストが 20 分走った後で、テスト用データベースの migration が古いと分かり、パイプラインを最初から再実行した
- [retry:rerun-after-late-check] デプロイ前の smoke test で設定ファイルの必須キーが無いと分かり、build からやり直した
- [friction] テストの失敗が環境の問題かコードの問題かを、ログを読むまで区別できない

## design-doc-review | documentation
- [retry:rerun-after-late-check] 設計文書をレビューに出した後で、前提にした要件票が旧版だと分かり、文書を差し戻して書き直した
- [manual_intervention] レビュアーが用語の不統一を 1 件ずつ手で指摘した
- [unexpected_success] 目次だけを先に見せたところ、前提の食い違いが本文を書く前に見つかった
