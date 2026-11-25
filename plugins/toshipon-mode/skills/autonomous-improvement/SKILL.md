---
name: autonomous-improvement
description: 成果物（コード・設計書・IaC・運用手順）を、仮説・実行・評価・採否の台帳を残しながら反復改善する。目的・制約・ベースライン・合格条件・予算を最初に固定し、採否は決定論的な評価で決め、停滞と完了時には独立監査にかける。Use when asked to keep improving an artifact against a measurable goal, to run an improvement loop with a hypothesis ledger, or to resume one.
argument-hint: "[run id | 対象プロジェクトのパス]"
---

# 成果物の改善ループ

Observe → Hypothesize → Execute → Evaluate → Decide を、台帳に残しながら反復する。判断は agent が行い、記録・測定・採否・状態遷移は同梱の `scripts/harness.py` が行う。手順の詳細、設定、停止方法は [README](README.md) を読む。

状態はどこにも保持しない。すべてのコマンドは追記専用の台帳を畳み直して次の行動を決めるので、セッションが切れても `run next` から再開できる。台帳は repository の外（既定 `~/.local/state/ai-harness/`）に置かれる。

Harness 自体（Skill・指示・評価）の改善は扱わない。それは `harness-evolution` skill の仕事である。マージ、デプロイ、本番計測もしない（`loop-engineering` の領分）。

## 始める前に固定するもの

開始時に次を spec（`schemas/spec.schema.json`）へ書き、人と合意する。合意していない項目があるうちは `run init` しない。

- 目的と制約。変更してはいけないファイルは `guard.protected_paths` に書く。
- 評価器。`evaluation.command` は argv で渡す外部コマンドで、指標を JSON で返す。設計書の要件トレーサビリティ検査や IaC の静的検証は、この形の adapter として書く。評価器のファイルは `evaluation.files` に並べる。内容が criteria hash に固定される。
- 指標。`required`（維持する条件）と `scored`（良くしたい値）を分ける。決定論的に測れるものを優先する。LLM による採点は `kind: llm` とし、根拠（rationale）を必ず返させる。LLM の点だけでは採用にならない。
- 合格条件（`acceptance.targets`）と、回帰を見るコマンド（`guard.checks`）。
- 上限（`budget` の試行回数・時間・費用）。

対象は git 管理下に置く。そうでないと、失敗した実験の変更だけを安全に戻せない。

## 1 回の反復

1. `run next <run>` を実行し、返った行動だけを行う。`baseline`、`hypothesize`、`execute`、`finish`、`audit`、`resolve_findings`、`stop` のどれかが返る。
2. `hypothesize`: 観測（評価結果、失敗理由、監査の指摘）から仮説を 1 つ書き、`hypothesis add` で登録する。根拠（rationale）と、どの結果なら仮説を捨てるか（falsification）を実験の前に書く。棄却済みの仮説とほぼ同じ文面は、新しい証拠（new_evidence）が無ければ拒否される。
3. `execute`: `experiment start` の後で変更する。1 つの実験では 1 つの仮説だけを試す。評価器と保護対象には触れない。実験の前から未コミットだった利用者のファイルにも触れない。
4. `finish`: `experiment finish` が、変更の検出、回帰チェック、測定、採否、差分の保存、失敗時の差し戻しをまとめて行う。採否は agent が決めない。採用できる結果を保守的に退けたいときだけ `--downgrade-reason` を使う。逆方向の上書きは無い。
5. 採否と理由を読み、次の `run next` に進む。

`finish` の判定は次の順で決まる。利用者の未コミット変更に触れた、または HEAD が動いた → `needs_human`（差し戻さない）。評価器・保護対象に触れた → `reject`（criteria_tamper）。何も変更していない → `reject`（no_change）。評価できない → `unevaluable`。ベースラインで通っていた回帰チェックや必須条件が落ちた → `reject`。scored の悪化が測定誤差を超えた → `reject`。決定論的な scored の改善が誤差を超えた → `adopt`。それ以外 → `reject`（below_noise）。

## 監査

`run next` が `audit` を返したら、`improvement-auditor` agent を**別のコンテキスト**（subagent か別プロセス）で起動し、台帳と差分を渡す。返った JSON を `audit record --independence subagent` で記録する。同じコンテキストで自己点検しただけなら `same_context` と記録する。それは完了の gate を通らない。

監査が起動するのは 2 つの場合である。

- 停滞: 同じ失敗理由が 3 回、または採用の無い試行が連続 3 回（どちらも spec の `plateau` で変更できる）。評価できなかった試行は数えない。
- 完了: 合格条件を満たした。監査を通るまで `passed` で止められない。

監査の指摘も主張であり、そのまま従わない。critical / high の指摘は 1 件ずつ証拠で確かめ、`audit resolve` で `confirmed`、`refuted`、`unverifiable` のどれかを記録する。監査が「合格条件が不適切」と指摘しても、agent は条件を変えない。条件を変えられるのは人だけで、`run criteria-change --reviewer <人の名前>` で記録し、ベースラインを取り直す。

## 止める

`run next` が `stop` を返したら、その理由で `run stop` する。台帳が支持しない理由では止められない（未達を `passed` にできない）。

| 理由 | 意味 |
|---|---|
| `passed` | 合格条件を満たし、独立監査に未解決の重大指摘が無い |
| `budget_exhausted` | 試行回数・時間・費用の上限に達した |
| `no_improvement` | 監査の後も停滞した |
| `unevaluable` | 評価できない実験が続いた |
| `needs_human` | 利用者の変更との衝突など、人の判断が要る。人が `run unblock` を記録したら、ベースラインを取り直してから続ける |
| `permission_denied` | 権限が足りず進められない（agent が申告する） |

最後に `run report` の Markdown を示す。報告では、合格したか、未達なら何が残ったかを最初に書く。測定値は台帳の値をそのまま使う。

## してはいけないこと

- 評価器、合格条件、回帰チェックを緩めて合格にする。
- 台帳を編集する。hash chain が切れ、以後のコマンドが拒否される。
- 失敗した実験を記録せずに捨てる。失敗も台帳に残す。
- 認証情報、顧客名、内部 URL を仮説や報告に書く。秘密情報の形をした文字列は記録時に伏せ字になるが、最初から書かない。
- 人の承認（`--reviewer`）を自分の名前で記録する。
