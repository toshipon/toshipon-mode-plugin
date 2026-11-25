# 発見ループの各段

モデルが返すのは各段の JSON だけである。何が残るかは `runtime/insight_discovery/checks.py`、だれの仕事かは `routing.py`、何を提示するかは `ranking.py` が決める。落ちた項目は理由とともに台帳に残り、レポートの「見送ったもの」に出る。

| 段 | 入力 | モデルが書くもの | 決定論的に行うこと | 落ちる理由 |
|---|---|---|---|---|
| 取り込み | アダプタの出力 | なし | 伏せ字、禁止語のマスク、内容 hash による ID と重複排除、件数からの信号抽出 | `excluded_*`、`format_unrecognized` |
| observe | 証拠と信号 | 観測（証拠 ID、引用）と解釈 | ID の実在、引用が原文の部分文字列であること | `no_evidence` `unknown_evidence` `quote_not_in_source` `quote_too_short` `unsupported_novelty_claim` `interpretation_without_observation` |
| structure | 残った観測 | 群、抽象化した問題、3 段の問い直し、疑う前提 | 群の証拠件数と業務数の算出 | `insufficient_evidence` `single_context` `unknown_observation` |
| hypothesize | 残った群、既出の仮説 | 仮説、作り方、類推、反証条件、不利な証拠 | 作り方の種類数、反証条件、既出との類似、新規性の断定 | `single_generator` `no_falsification` `analogy_missing` `duplicate_of_rejected` `duplicate_of_existing` `duplicate_in_batch` `unsupported_novelty_claim` |
| audit | 残った仮説、隣の skill の範囲 | 既存で足りるか、問題設定、反証可能性、指摘、勧告 | 監査の独立性、振り分け（外に出す向きだけ） | `not_audited` `audit_without_basis` `finding_without_evidence` `audit_not_independent` `rejected_by_audit` `not_falsifiable` `misframed` `existing_method_sufficient` |
| assess | 研究課題として残った仮説 | 4 軸の序数と理由、最小実験、成功・棄却条件、確信度、未確認事項 | 欄の完全性、新規性の断定、証拠の軸の算出、順位付け、上位 3 件 | `incomplete_experiment` `score_without_reason` `not_assessed` `unsupported_novelty_claim` `below_cut` |

## 語彙

- 観測の反復の強さ（`evidence_summary.strength`）: `candidate`（1 業務で 1、2 回）、`recurring`（2 業務以上、または 3 回以上）、`strong`（3 業務以上かつ 5 回以上）。証拠から計算する。
- 仮説の確信度（`confidence`）: `low` / `medium` / `high` と理由。数値は持たない。
- 新規性（`novelty.status`）: `not_searched`、`searched_none_found`、`partial_overlap`、`covered_by_existing`。記録された検索から導く状態で、モデルは書けない。
- 状態（`status`）: `candidate` → `accepted`（人の選択）→ `tested` / `rejected`。`candidate` → `rejected` もある。`tested` と `rejected` の後は変更できない。

## 順位付け

5 軸（value、evidence、delta_from_prior_art、testability、cost）の 1〜3 を合計し、大きい順に並べる。同点は、証拠の軸、業務数、fingerprint の順で決める。evidence の軸は反復の強さから計算する。delta_from_prior_art は、既存手法の検索が記録されていない間は 1 に固定する（モデルの見立ては理由として残る）。残りの 3 軸はモデルが理由付きで付ける。cost は 3 が最も安い。

## 予算と再開

1 回の実行は、モデルの呼び出し回数（`max_llm_calls_per_run`）、経過秒数（`max_seconds_per_run`）、土台の 1 日の費用上限（`budget.max_cost_per_day`）のどれかに達すると、`budget_exhausted` を台帳に書いて止まる。次の実行は、記録済みの段の次から進む。段の途中で落ちた場合も同じで、記録されていない段だけがやり直される。
