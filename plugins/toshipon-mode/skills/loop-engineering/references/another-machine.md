# 別の端末でループを動かす

ループを動かす端末は**常に 1 台だけ**にする。2 台が同じ main にマージすると、どちらの checks も
相手の変更を見ていない。同じ metric に 2 本の仮説が同時に走り、交絡を防ぐための
`cap_concurrent_measuring` が意味を失う。

移行するなら、**先に移行元を止める**。

```bash
bash <scripts>/loop-install.sh --uninstall <loop_id>
launchctl list | grep com.toshipon.loop     # 消えていること
```

同じ repo に別のループ（研究用など）がある場合は `shared_lock` を共有する。片方だけが動いている
端末でも、lock は単独の mutex として働く。

**`shared_lock` は端末を跨がない。** ファイルシステムの mutex なので、2 台が同時に動いていても
互いを見ない。それを検知できるのは `status_db` だけで、`loop-status.sh` が 24h に 2 つ以上の host を
見たら警告を出す。移行の前後はここを見る。

## 1. 必要なもの

| | 確認 |
|---|---|
| Claude Code | `claude --version`。ログイン済みであること |
| git / gh | `gh auth status` が `repo` scope で通ること |
| jq | `jq --version` |
| repo のツールチェーン | `repo_check` と `surface_checks` が走る環境（uv / node など） |
| デプロイ確認のコマンド | `deploy_verify` が返すこと（Cloudflare なら `wrangler` のログイン） |
| 1Password CLI | Slack 通知を使う場合のみ |
| wrangler の認証 | `status_db` を使う場合のみ。`npx wrangler d1 list` が通ること |

macOS 前提である。launchd と `osascript` を使う。

## 2. repo

```bash
git clone <repo> && cd <repo>
<setup_command と同じもの>      # loop.yaml の setup_command
<repo_check と同じもの>          # 全部 pass すること
```

ループ用の worktree は `loop-run.sh` が `<repo>-loop` に自動で作る。別の場所に置きたいなら
`LOOP_WORKTREE` を機械ごとの env で指定する。

## 3. 端末ごとの設定

launchd は素の PATH で起動する。既定は `~/.local/bin`、nodenv の shims、`/opt/homebrew/bin` など
である。違う場合は repo の外に置く。

```bash
mkdir -p ~/.config/loop-engineering
cat > ~/.config/loop-engineering/<loop_id>.env <<'EOF'
LOOP_PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
LOOP_WORKTREE="$HOME/src/<repo>-loop"
# LOOP_SLACK_WEBHOOK=""   # 空にすると Slack を止める
EOF
```

各バイナリの場所は `command -v` で確認する。シェル関数として出る場合（anyenv の lazy init）は
shims の実体パスを探す。

## 4. 導入前の確認

```bash
S=~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/skills/loop-engineering/scripts
export LOOP_REPO=$(git rev-parse --show-toplevel)

bash $S/loop-selftest.sh                     # 設定の読み取りと境界の照合
bash $S/loop-status.sh $LOOP_REPO            # 盤面が出ること
bash $S/loop-metrics.sh list                 # metric が列挙されること
bash $S/loop-check.sh                        # repo の checks が green
```

`loop-metrics.sh metric <key> <from> <to>` で数字が返ることも見る。返らない metric があるなら、
まだ計測が動いていない。

## 5. 登録

```bash
bash $S/loop-install.sh $LOOP_REPO 15 30        # 毎日 15:30
bash $S/loop-install.sh $LOOP_REPO "*/3" 30     # 3 時間ごと（00:30 から）
launchctl list | grep com.toshipon.loop
```

署名（`approved_by` / `approved_at`）と health が通らないと、launchd に何も書かずに拒否する。

**分は他の cron とずらす。** 同じ端末やリポジトリの worker が毎時 :05 に動くなら、ループは :30 に
する。`avoid_minutes` はゲート側の保険であって、そもそも重ねない方がよい。

## 6. plugin を更新したら再登録する

plist が指すパスには plugin のバージョンが入っている。更新すると新しいバージョンのディレクトリが
増えるだけで古い方は残るので、**launchd は古いスクリプトを黙って走らせ続ける**。

```bash
claude plugin update <plugin>@<marketplace>
bash <新しい version の scripts>/loop-install.sh $LOOP_REPO "*/3" 30
```

## 7. 動いているかの確認

| 見たいもの | 場所 |
|---|---|
| tick ごとの digest | Slack（`slack_webhook_op` を設定した場合）、`~/Library/Logs/<loop_id>/digest.log` |
| tick の生ログ | `~/Library/Logs/<loop_id>/tick-*.json` |
| 盤面 | `loop-status.sh <repo>` |
| 何をしたか | `<records_dir>/journal.md`。記録を動かした tick だけが書く |
| 人間待ちの PR | GitHub の `needs-human` ラベル |
| どの端末が回しているか | `loop-status.sh <repo>` の最後の行（`status_db` を設定した場合） |

止めるときは `<records_dir>/paused.flag` を commit する。次の tick が決定表の行 0 で止まり、人間が
消すまで動かない。端末から外すなら `loop-install.sh --uninstall <loop_id>`。
