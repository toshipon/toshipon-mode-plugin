### Session pickup

**resume point はあなたが持つ。以前の trail を読み、やり直さない。** 「これを引き継いで」「この会話を再開して」「<transcript path> から続けて」「あなたが引き継ぐ」「X が中断したところから拾って」向け、あるいは自分が続けるべく push されたブランチ向け。

pickup は継承である。以前の agent はすでにコードを読み、repro を実行し、設計の選択をするコストを払った。やり直すと bias check を失い context を燃やす。再導出したい衝動には抗い、読む。

1. 以前の trail を見つける。**Pause safely** が残した resume note、**show-me-your-work** の decision trail、`~/.claude/projects/<encoded-cwd>/*.jsonl` 下のトランスクリプト（`<encoded-cwd>` は workspace の cwd の `/` を `-` に置き換えたもの。他の workspace の境界を越え無関係なプロジェクトの private chat を読んでしまうので、`~/.claude/projects/` 下の他のディレクトリを横断して glob しない）、または push されたブランチ。まずメタデータの概要と最後のメッセージを読み、次に決定ポイントまで遡ってスキャンする。長いトランスクリプトは subagent で parse し、圧縮したタイムラインだけを main thread に保つ（[guard-the-context-window](../references/principles/guard-the-context-window.md)）。
2. 動作状態を再構築する。ブランチと worktree、すでに着地したもの（`git log`、base に対する `git diff`）、未解決の todo、下された判断。以前の trail が権威ある入力である。それを再導出したいという偏りには抗う。
3. done と pending を diff する。出荷されたものを計画されたものと比較し、resume point を名指しし、以前の repro を再実行したり完了した作業をやり直したりしない。「念のためゼロから検証させて」というパスは、権威あるはずの trail を信用していないことの兆候である。
4. 残りの作業を一致する playbook にルーティングし、判定を選ぶ。実行を続ける、完成した推奨を出荷する、以前の結論を追認または覆す、失敗した run を postmortem する。pickup playbook はここで終わる。ルーティングされた playbook が残りを持つ。
5. 継承した主張を、元のゴールに対して実際の成果物で検証する（[prove-it-works](../references/principles/prove-it-works.md)）。以前の自己報告が通っていることは証明にならない。

**Reply:** 以前の agent がどこで止まったか、何を継承し何をやり直したか（理想はやり直しゼロ）、resume point、結果。
