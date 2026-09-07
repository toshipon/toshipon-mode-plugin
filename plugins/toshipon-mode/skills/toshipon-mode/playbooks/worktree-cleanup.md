### Worktree and simulator cleanup

**ディスクと安全ゲートはあなたが持つ。** マージ済みまたは放棄された git worktree と古い iOS simulator を刈り込み、容量を回収する。削除は不可逆なので、使用中のものや未commitの作業を削除しないよう、あらゆるステップが防御している。

1. スナップショットを撮り監査する。まず `df -h /` を記録し、`git worktree list --porcelain` から監査テーブルを作る。手打ちのパスからは絶対に作らない。手打ちの `myrepo-worktrees/x` は、`~/.superset/worktrees/<repo>/x` にあるものを見落とすからである（[encode-lessons-in-structure](../references/principles/encode-lessons-in-structure.md)）。各 worktree について、サイズ（`du -sh`）、最後の commit の age、デフォルトブランチに対するマージ状態、未commitの作業（`git -C <path> status --porcelain`）、PR の状態（`gh pr list --head <branch>`）、それに触れた `~/.claude/projects/` 下の最新セッションを記録する。それぞれを分類する。`safe`（マージ済み、clean、open PR なし）、`verify-recent-chat`（最近のセッションが触れた）、`wip:N`（N 個の追跡された未commit編集）、`scratch:N`（N 個の未追跡ファイル）。監査は場当たりのコマンドではなく再利用可能な script として書き、遅いので session scan はバックグラウンドで行う。
2. 分類は許可ではなくアドバイスである。pin されたセッションとアクティブなセッションが本物の成果物である（[prove-it-works](../references/principles/prove-it-works.md)）。この集合はユーザーから、または実行中のセッション一覧から得て、すべての候補と突き合わせる。監査はユーザーが実際に保持している worktree を `safe` とマークしてしまうことがある。ユーザーの集合が優先する。
3. 削除する前に使用状況を検証する。すべての `verify-recent-chat` の行、または少しでも疑わしいものについて、subagent を fan out してトランスクリプトを読ませ、セッションが進行中かどうかとどの worktree に触れているかを報告させる（[guard-the-context-window](../references/principles/guard-the-context-window.md)、トランスクリプトは bulk である）。アクティブなセッションはバックグラウンドの subagent 経由で比較や repro 用の tree を兄弟 worktree として spawn しており、それらの名前がセッション一覧に一度も現れなくても使用中である。
4. 不可逆な損失で一時停止する。`wip:N` は N 個の追跡された未commit編集である。diff を見せて先に判断を得る。clean な worktree の削除はブランチから復旧できるが、未commitの作業は消える。`scratch:N` は未追跡の使い捨てで削除して安全だが、ファイル名は名指しする。Autonomy に従い、clean でマージ済みで未使用のものは進める。`wip` と使用中のものは一時停止する。
5. 確認済みの集合を刈り込む。パスごとに `git worktree remove --force <path>`。dir が ignore された build artifact のせいで残っている場合は `rm -rf` し、その後 `git worktree prune`。ブランチの ref は残るので commit は失われない。`df -h /` と再list で確認する。
6. simulator とその他の回収手段。simulator は通常次に大きい効果を持つ。`xcrun simctl --set testing delete all`（XCTestDevices の clone）、`xcrun simctl delete unavailable`、そして `xcrun simctl runtime list` の後に古い runtime に対して `runtime delete <id>`。必要に応じてさらに、Xcode の `DerivedData` と `iOS DeviceSupport`、editor の application-support キャッシュ、`~/.claude/shell-snapshots/` と古い `~/.claude/projects/` のトランスクリプト（Session pickup が読む最近のものは残す）、パッケージキャッシュ（pnpm、uv、brew、yarn）。ユーザーが残すよう言っていないキャッシュだけをクリアする。

この playbook は、それを捕まえるコードレビューなしにユーザーの状態を削除する唯一のものなので、上記のゲートがレビューの役割を果たす。

**Reply:** 前後の `df -h /` と回収した容量、刈り込んだ worktree、そして残した各 worktree の 1 行の理由（どのセッションが使用中か、または未commitの作業があるか）。
