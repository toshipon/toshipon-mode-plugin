# NFR と Doc workspace の契約

台帳が正本です。`input.json` はその run で使う要求の写し、設計 JSON は候補、Markdown は設計 JSON の view です。正本の更新時は入力を更新し、新しい run で再評価します。

## 必須ファイル

workspace に `sources.tsv`、`facts.md`、`questions.tsv`、`decisions.tsv` を用意します。symlink は受け付けません。sources.tsv と questions.tsv は既存 Doc workspace と同じ列順・タブ区切りです。出典 ID、質問 ID の重複を拒否します。

sources.tsv は `id kind locator title retrieved note` の 6 列です。locator、title、retrieved は空にできず、retrieved は ISO 日付にします。technical_evidence の URL も sources.tsv の locator に登録します。

questions.tsv は `id question blocks owner status asked_at answer evidence incorporated_in updated` の 10 列です。`blocks` に要件 ID または単語 `nfr` を含む質問が対象です。章単位の質問にも `nfr` を追加してください。open / asked / assumed は合格を阻みます。answered / incorporated は回答と、sources.tsv に存在する S ID の evidence が必要です。質問の変更は既存の question.sh で行います。

## 要件と単位

| metric | category | unit | 判定方向 |
|---|---|---|---|
| availability_percent | availability | percent | 下限 |
| p95_latency_ms | performance | ms | 上限 |
| rto_minutes | recovery | minutes | 上限 |
| rpo_minutes | recovery | minutes | 上限 |
| encryption_percent | security | percent | 下限 |
| detection_minutes | operations | minutes | 上限 |
| monthly_cost_usd | cost | USD/month | 上限 |

metric と category / unit の組み合わせを固定します。未対応の指標を黙って別指標に読み替えません。要件 ID は `REQ-001` 形式、設計判断は `D1`、検証計画は `V1`、根拠は `E1` 形式です。RTO/RPO の意味を ID の文字列から推測しません。

description に負荷、期間、障害対象、適用範囲を記載します。Python は target / unit と数値上限を検査し、文章中の測定条件と方向の一致は監査者が確認します。

## 確認済み根拠

technical_evidence は id、url、claim、status、verified_by、checked_at を持ちます。url は AWS 公式 HTTPS host のみ、checked_at は ISO 日付です。本文を確認した担当者が verified にします。生成者は入力 registry と異なる根拠や確認状態を返せません。

この仕組みは署名や権限システムではありません。虚偽の入力を見破る保証はなく、本文取得・claim の真偽判定は自動化していません。レジストリと台帳の claim の妥当性は独立監査でも確認します。

## 候補と監査

設計候補は requirements、decisions、evidence、verifications、aws、open_issues、assumptions を持ちます。全要件に設計判断と検証計画が必要です。AWS の RTO/RPO、費用を要件と照合します。重要な未確定は high の open issue として記録します。

監査は findings と limitations を持ちます。findings の critical/high は合格を阻み、medium/low は記録として残します。重大な不足を limitations へ移して通してはいけません。

## 成果物の形

requirements.md は reference として要求と出典を記載します。design.md は構成と決定・代替案を記載し、各行を REQ と根拠へ対応させます。verification.md は方法、合否条件、評価の限界を記載します。3 文書とも How to read this、前提、未決事項、Appendix A. 出典一覧を持ちます。

既存 doc-check は出典札と REQ の対応を検査します。NFR 評価器は、これに数値・Schema・根拠 registry の検査を加えます。両方を通しても実測達成や業務承認は意味しません。
