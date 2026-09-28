---
description: 仮説検証ループの盤面を出す。state 別の件数、各 record が何待ちか、次の tick が取る行動、劇場チェックの比率
---

# /toshipon-mode:loop-status

`loop-engineering` skill を Skill tool で読み、盤面を出す。書き込みは一切しない。

## Step 1: 盤面

```
${CLAUDE_PLUGIN_ROOT}/skills/loop-engineering/scripts/loop-status.sh .
```

## Step 2: 次の行動を判定する

skill の決定表を上から評価し、次の tick が取る行動を 1 つ名指しする。なぜその行が最初に一致したかも
書く。判定に必要なら `loop-metrics.sh` で実際の数字を読む。推測で書かない。

## Step 3: 待ちの理由

`measuring` の record それぞれについて、あと何日で horizon に達するか、現在の n が
`cap_min_sample` に届く見込みかを書く。届かない見込みなら、その record は `inconclusive` で
終わる。今のうちにそう言う。

## Step 4: 劇場チェック

terminal に達した record のうち `inconclusive` の比率を出す。50% を超えていたら、それはこの
ループが機能していない証拠である。metric 設計を変えるか止めるかの判断が要ると書く。

terminal が 3 件未満なら「まだ判定できない」と書く。少ない標本で比率を語らない。

## 出力

日本語で書く。表 1 枚と、次の行動 1 行、待ちの理由、劇場チェック。それ以上は書かない。
