---
name: harness-evolution
description: 実行記録に繰り返し現れる失敗から、Skill・指示・ツール・評価の改善仮説を立て、現行版を固定して隔離 worktree の候補版と同じ条件で比較する。事前に決めた改善条件と独立監査を通った候補だけを、ローカルのレビュー用提案にする。公開とマージは人の承認が要る。Use when asked to improve a skill, prompt or harness from recorded failures, to compare a candidate harness against the current one, or to review an evolution proposal.
argument-hint: "[EH-NNNN | P-NNNN]"
---

# Harness のメタ改善

成果物ではなく、成果物を作る側（Skill、指示、ツール、評価）を改善する。`autonomous-improvement` の改善ループとは別の台帳（`evolution/ledger.jsonl`）を使い、同じ launcher（`../autonomous-improvement/scripts/harness.py`）の `evolve` サブコマンドで進める。手順と設定は [autonomous-improvement の README](../autonomous-improvement/README.md) にある。

この skill が出すのはローカルのレビュー用提案までである。変更を適用しない。公開用ブランチも PR も作らない。

## 前提: 人が承認した評価 suite

比較には評価 suite（`schemas/evolution.schema.json` の `suite`）が要る。suite は対象 repository、基準の ref、runner、評価ケース、train / holdout の分割、改善条件を持つ。runner とケースは suite のディレクトリの中に、suite は対象 repository の外に置く。中に置いた suite は承認できない（候補版から holdout が読めてしまうため）。

suite を使えるようにするのは人である（`evolve suite-approve --reviewer <人の名前>`）。内容の hash が記録され、ケース・runner・改善条件・分割のどれを変えても、再承認するまで比較は拒否される。agent は suite を編集しない。変更が必要なら理由を添えて人に依頼する。

holdout ケースは仮説と候補を作る材料にしない。読まない。仮説の文面に holdout ケースの ID があると登録は拒否される。

## 手順

1. **分析する。** `evolve analyze` が、改善ループの台帳（棄却理由、確認済みの監査指摘）と hook の記録（ツール失敗）を失敗パターンに畳む。既定では 3 件以上かつ出所 2 つ以上のパターンだけが対象になる。1 件だけ、1 つの出所だけの失敗は `candidate` のままで、仮説を立てられない。対象が無ければ何もしない。
2. **仮説を登録する。** 原因を skill、instruction、tool、evaluation、input、environment に分類し、仮説と反証条件を書いて `evolve hypothesis-add` で登録する。この時点で現行版の commit が基準として固定される。同じ証拠で 2 度目の仮説は立てられない。
3. **候補版を作る。** `evolve candidate-create` が、基準の commit から切った worktree を状態 home の中に作る。編集はその中だけで行う。利用者の checkout には触れない。仮説が指す 1 点だけを変え、`evolve candidate-freeze` で差分を固定する。固定後に候補を変えると比較は拒否される。
4. **比較する。** `evolve compare` が、現行版と候補版を全ケース・同じ試行回数で、順序を入れ替えながら実行する。初回成功率、失敗再発率、修正回数、実行時間、トークン、費用、品質を、平均とばらつきと試行回数付きで記録する。
5. **判定を読む。** 判定は suite の改善条件だけで決まる。
   - `reject`: 現行版で必ず通っていたケースや必須条件が候補版で落ちた。または train / holdout が誤差を超えて悪化した。
   - `hold`: runner の失敗、ケース数や試行回数の不足、holdout の改善が条件か誤差に届かない。証拠不足は保留であり、条件を下げて通さない。
   - `propose`: holdout の改善が、事前に決めた幅と測定誤差の両方を超えた。
6. **独立監査にかける。** `improvement-auditor` agent を別のコンテキストで起動し、仮説・比較結果・差分を渡す。結果を `evolve audit-record` で記録する。critical / high の指摘は証拠で確かめ、`evolve audit-resolve` で記録する。refuted にならない重大指摘が残る候補は提案にできない。
7. **提案を作る。** `evolve propose` が `evolution/proposals/P-NNNN/` に report.md、proposal.json、candidate.patch を書く。報告は差分、測定結果、リスク、ロールバック手順を含む。

## 人の承認が要る操作

次は agent が実行しない。人が判断し、人の名前で記録する。自動実行中のプロセスからは記録できない。

- 評価 suite の承認と変更（ケース、holdout、改善条件）。
- 提案の承認（`evolve approve`）。
- 公開。`evolve publish-check` は、承認の記録、許可リスト（`publish.allow_paths`）、秘密情報の検査、禁止語の検査を通った場合だけ、ブランチと PR を作るコマンドを表示する。実行はしない。許可リストに無いパス（移動・削除の元も含む）、symlink、mode の変更、バイナリは遮断される。
- マージ。PR の承認とは別に判断する。

## 日次の自動実行

`schedule-tick`（launchd から、または手で）が `evolve` job を 1 日 1 回キューに入れ、上の手順を adapter で進める。繰り返すパターンが無い日は adapter を起動せずに終わる。仮説・候補・監査の adapter が設定されていない、またはデータ分類（suite と、パターンの出所のうち厳しい方。hook の記録は `internal`）が `llm_policy` で許可されていない場合は進まない。1 日の費用上限に届く呼び出しは始めず、job は翌日まで待つ。途中で止まった仮説は、次の実行が台帳の段階から再開する。

`schedule-tick --dry-run` は、状態 home の写しの上で同じ処理を通し、実際の状態には書かない。費用のかかる adapter と、`dry_run_safe` でない runner は起動せず、起動するはずだった内容を報告する。
