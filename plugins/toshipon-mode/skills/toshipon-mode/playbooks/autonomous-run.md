### Autonomous run

**終了条件はあなた自身が保有する。done を定義してから、止まらずにそこへ駆動する。** 「寝る」「run until done」「/loop until X」向け。

1. 最初のイテレーションの前に、終了条件をチェック可能な述語として述べる（テストが green、repro が直った、N 個の PR すべてがマージされた、pixel-diff がゼロ）。曖昧な目標は行き詰まる。述語があれば止まれる。
2. 承認が要る行動を先に列挙する。run 中に必要になりそうな不可逆な行動（`terraform apply`、デプロイ、repo 作成、force-push、削除）を最初のイテレーションの前に一覧にし、ユーザーに一度だけ pre-approve / veto を求める。veto されたもの、または途中で permission に止められたものは、停止せずに `./handoff/<name>.sh` へ実行可能なスクリプト（各コマンドの blast radius をコメントで添える）として書き出し、run を続ける。gated な行動は run の最後にまとめて一度だけ提示する。
3. Claude Code 組み込みの `/loop` を使って wake の仕組みを選ぶ。監視すべきイベント（CI、マージ、ref の前進）があれば、そのイベントで起こす watcher subagent を用意し、フォールバックとして長い時間ベースの heartbeat を添える。イベントがない場合は、結果を再確認する価値があるタイミングに合わせた固定間隔の heartbeat にする。CI については具体的に `/check-github-ci` が watcher になる。
4. 各イテレーションでは、証拠が正当化する最小の変更を行い、述語に対して検証し、前進すれば commit し、役に立たなかった変更は破棄する。「役に立つかもしれない」belt-and-suspenders は revert し、そのまま乗せておかない。
   [sequence-verifiable-units](../references/principles/sequence-verifiable-units.md) に従って作業を並べ、最後にまとめて確認するのではなく各 unit を次に進む前に検証する。
5. 途中で見つかったものはあなたのものである。壊れた skill、関連するバグ、flaky な verifier、レビューのノイズ、tooling の失敗、放置された follow-up、修正可能な drift は toshipon-mode 経由で自分で対処する。範囲外の修正は独立した PR にする。可逆的な作業を人間のために取り置いたり、`AskUserQuestion` を使ったりしない（[never-block-on-the-human](../references/principles/never-block-on-the-human.md)）。表に出すのは、不可逆な行動、実験では決着しない本物のプロダクトや好みの判断、または本当の行き詰まりだけである。述語を主要な駆動力として保ち、寄り道の修正のたびにそこへ戻る。
6. **show-me-your-work** skill で毎イテレーション checkpoint する。何が変わったか、述語が動いたかどうかの行を記録する。記録のない run は監査も再開もできない。
7. 述語が満たされたら止まる。停滞は停止ではない。止まらずアプローチを転換して押し進める。行き詰まったらスピンするのではなく、本物の dead end として表に出す。勝利を宣言するために述語を緩めることは決してしない。

イテレーションの間、main thread は薄く保つ。大量の出力は subagent に流し、要約だけを保持する（[guard-the-context-window](../references/principles/guard-the-context-window.md)）。context が compact されそうなときは、先に **Pause safely** を実行し、run が要約を生き延びるようにする。

**Reply:** 終了条件、実行したイテレーション数、何が着地したか、何が破棄されたか、`./handoff/` に残した gated スクリプトの一覧、述語の最終状態。並行して動く operator がこの run の状況を把握できるよう、`CLAUDE.md` に従った session-status block を含める。
