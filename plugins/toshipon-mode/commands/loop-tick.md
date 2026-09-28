---
description: 仮説検証ループを 1 tick 手で回す。headless と同じ決定表・同じ gate を使う。--dry-run は行動の判定だけ出して止まる
argument-hint: "[--dry-run]"
---

# /toshipon-mode:loop-tick

ループを 1 tick 実行する。headless の `loop-run.sh` と同じ決定表、同じ gate、同じ journal 書式を
使う。対話でやることの意味は、INTERVIEW でブラウザを動かせることと、結果をその場で見られること
だけである。判断を緩めない。

`loop-engineering` skill を Skill tool で読み、`references/cycle-prompt.md` に書かれた手順を
そのまま実行する。ただしスクリプトのパスは placeholder ではなく実体を使う。

```
SKILL_DIR = ${CLAUDE_PLUGIN_ROOT}/skills/loop-engineering
METRICS   = $SKILL_DIR/scripts/loop-metrics.sh
CHECK     = $SKILL_DIR/scripts/loop-check.sh
PUSH      = $SKILL_DIR/scripts/loop-push.sh
MERGE     = $SKILL_DIR/scripts/loop-merge.sh
```

これらは `LOOP_REPO` を要求する。実行前に `export LOOP_REPO=$(git rev-parse --show-toplevel)`
する。

## 手順

1. `loop-status.sh .` で state を読む。`paused.flag` があれば、なぜ止まっているかを報告して
   終わる。人間が消すまで進めない。
2. `deploy_health` が設定されていれば実行する。異常なら GUARD に入る。
3. 決定表を上から評価し、一致した 1 行だけを実行する。
4. `$ARGUMENTS` に `--dry-run` があれば、選んだ行動とその根拠、次に触るファイル、計測する metric を
   書いて止まる。record もコードも変更しない。
5. BUILD なら `toshipon-mode` skill の Feature playbook に入る。INTERVIEW なら surface を
   `claude-in-chrome` か `superset:browser` で実際に操作してからインタビューを書く。
6. journal を書く。cycle-prompt.md step 6 の 2 段構えの書式をそのまま使う。
7. ship するなら CHECK → commit → PUSH → `gh pr create` → MERGE の順で、この経路だけを使う。
   `git push` と `gh pr merge` は使わない。

## 報告

実行した行動、動いた record の state 遷移、計測した値と n、開いた PR の URL、次の tick の予測。
`CLAUDE.md` に従った session-status block で終わる。
