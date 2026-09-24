### Pause safely

**きれいな停止はあなたが持つ。cold-start の agent が再開できる checkpoint を残す。** 「pause safely」「オフラインになる必要がある」「Claude Code を再起動」「飛行機に乗る」向け、そして context が compact または summarize されそうなとき向け。これは明示的な場合のみ。「続けて」「寝るけど続けて」「止まるな」のときは pause しない。それらは続行を意味し、Autonomous run はすでにイテレーションごとに checkpoint している。

1. 安全な境界で止まる。現在の atomic step を終えるか、そこから抜ける。何も新しく始めず、ネストした subagent はすべてキャンセルする。
2. pause するために不可逆な一線を越えない。すでに出していた場合を除き、PR も push もしない。
3. 作業を永続化する。commit していない編集は、現在のブランチに 1 つの明確な `wip:` commit としてまとめ、何も失われないようにする。tree が壊れている場合は、commit body に一行でそう書く。worktree の中では bare な `git stash` を使わない。stash stack は他のセッションと共有されている。
4. resume note を context の外に書く。意図、何をしていたか、進捗と検証済みのこと、現在の状態、次のステップ、key files、gotchas を記録する。compaction が trigger の場合は、session scratchpad ディレクトリのファイル `<scratchpad>/<slug>-resume.md` に書く。in-context のプランは要約を生き延びないからである。show-me-your-work の trail が存在するなら、それを重複させるのではなく、そちらを指す。

**Reply:** ループのどこにいるか、ディスク上にあるものと頭の中にまだあるもの（パスのみ、diff のダンプはしない）、行った commit と tree が clean かどうか、resume 時の最初のアクション。これは pause であり、最終レポートではない。resume は Session pickup playbook がこの note を読むことである。`CLAUDE.md` に従った session-status block で締め、成果物行には resume-note のパスを書く。
