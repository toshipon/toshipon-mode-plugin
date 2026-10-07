---
description: リポジトリを loop engineering に載せる。前提を点検し、records 雛形と loop.yaml の草案と不変条件テストを置き、人間が埋める欄を列挙する
argument-hint: "[省略可。records_dir を変えたい場合はディレクトリ名]"
---

# /toshipon-mode:loop-init

このリポジトリで自律の仮説検証ループを回せるようにする。`loop-engineering` skill を Skill tool で
読んでから始める。既存ファイルは上書きしない。

`$ARGUMENTS` が空なら records_dir は `product` にする。

## Step 1: 前提を点検する

3 つ揃わないとループは動かない。足りないものは名指しして、この時点で報告する。

1. **マージがデプロイに届く経路。** Cloudflare Workers Builds、Vercel、GitHub Actions のいずれか。
   `git log` でマージ後にデプロイが走った証跡を探し、デプロイ済みの版を返すコマンドを特定する
   （Cloudflare なら `npx wrangler deployments status --json`）。無ければ `deploy_kind: manual`
   にして、そう報告する。
2. **agent が読める計測。** 既にイベント収集があるか。無ければ「`/hdd:data-infra` で設計が必要」と
   報告し、ループはここで止める。計測の無いループは劇場である。
3. **人間が承認する境界。** `allowed_paths` に何を入れるかは人間が決める。候補を提案するが、
   確定はしない。

同じリポジトリに別の自律ループがあるかを確認する（`scripts/autoloop/` のような場所）。あれば
その lock の path を見つけ、`shared_lock` に書く。2 つのループが同時に main にマージすると、
どちらの checks も相手の変更を見ていない。

## Step 2: 雛形を置く

- `<records_dir>/hypotheses/.gitkeep`
- `<records_dir>/measurements/.gitkeep`
- `<records_dir>/journal.md`（見出しと、記録の読み方を 3 行）
- `<records_dir>/metrics.yaml`（計測から実際に読める metric だけ。空でもよい）
- `<records_dir>/loop.yaml`（草案。`references/record-schema.md` の形に従う）

`loop.yaml` の `allowed_paths` には `loop.yaml` 自身と `metrics.yaml` を**入れない**。ループが
自分の境界と採点表を書けないことが、この設計の硬い不変条件である。

## Step 3: 不変条件テストを書く

`references/record-schema.md` の「不変条件」12 項目を、このリポジトリの toolchain で検査する
テストを 1 本書く。Python なら pytest、TypeScript なら既存の test runner。schema は skill が
定義し、強制はリポジトリが行う。

テストを `repo_check` に含める。指示文ではなく落ちるテストが境界を守る。

## Step 4: 最初の metric を確かめる

`${CLAUDE_PLUGIN_ROOT}/skills/loop-engineering/scripts/loop-metrics.sh list` と、1 つの metric に
対する `metric <key> <from> <to>` を実際に走らせる。数字が返らない metric は `metrics.yaml` に
書かない。読めない metric を書くと、最初の tick が空振りする。

各 metric の `expected_n_per_day` は推測ではなく、実際に読んだ値から書く。

## Step 5: 報告

- 前提 3 つの点検結果。足りないものと、それが無いと何が起きるか
- 置いたファイルと、人間が埋める必要のある欄（`approved_by`、`allowed_paths`、`metrics.yaml`）
- `loop-metrics.sh` が実際に返した値
- 次にやること。`/toshipon-mode:loop-status` で盤面を見て、最初の仮説を 1 本人間が書く

launchd への登録は提案までにする。`loop-install.sh` の実行はユーザーに任せる。
