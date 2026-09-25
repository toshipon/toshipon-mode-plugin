---
name: create-verification-skill
description: 実アプリを実際に起動・操作して証拠を残す repo 固有の検証 skill（.claude/skills/verify-<app>/）を生成する。PR 前の静的検証を回す verification-loop とは別物で、こちらは「動くことの証明」を自動化する仕組み自体を作る
---

# Create a verification skill

まともなプロジェクトには、本物のアプリを実際に動かして挙動を証明する scripted な方法が必要である。起動し、ユーザーがするように feature を操作し、証拠を残す。この skill は、それをリポジトリ固有の project-local skill（`.claude/skills/verify-<app>/`）として生成する。生成物は次の agent のために書く。人間のためではない。それは、そのアプリを一度も見たことのない agent によって、タスクの途中で初見で読まれる。

これは、PR の前にリポジトリの既存の static gate（build、typecheck、lint、tests、security）を走らせる **verification-loop** とは別物である。それらの gate は「feature が動く」よりも狭い主張をチェックする。この skill は、より広い主張をチェックする仕組みそのものを構築する。

## 1. Interview the repo, not the user

これらはコードベースから答え、観測できないことだけをユーザーに聞く。

- **Surface:** ユーザーが実際に触れるのは何か。web UI、CLI/TUI、desktop app、API、mobile app、library か。1 つのリポジトリが複数持つこともある。主たるものを選び、残りは書き留める。
- **Run:** アプリはローカルでどう起動するか。リポジトリ自身の文書化された dev コマンド（package script、Makefile、README の quickstart）を優先する。ポート、環境変数、seed データ、認証を記録する。
- **Drive:** agent はどうプログラム的に操作できるか。既存の harness をまず探す。Playwright/Cypress の spec、expect script、PTY helper、curl できる endpoint、debug port。それがなければ汎用のレシピを選ぶ。web と Electron には `claude-in-chrome` 経由のブラウザ、CLI/TUI には tmux/PTY harness、mobile には `e2e-replay` 経由の iOS simulator、service にはプレーンな HTTP。
- **Observe:** どんな証拠を捕捉できるか。スクリーンショット、terminal の transcript、response body、log、exit code、DB の状態。
- **Isolate:** 2 つのインスタンスを並べて動かせるか（ポート、データディレクトリ、プロファイル）。できない場合は、生成する skill にそう書く。共有インスタンスを二重に駆動することを拒否する方が、ユーザーのセッションを壊すより良い。

checkout がそのままではビルドや起動をしない場合は、生成の前にそれを直す（あるいは正確に報告する）。壊れた base に対して書かれた skill は、間違ったステップを教える。起動をブロックしている無関係な欠けている asset（API が決して配信しない static dir、サンプル設定）は、生成する skill が検証用の足場として明示した上で作成し、cleanup で削除してよい。

## 2. Generate the skill

`.claude/skills/verify-<app>/SKILL.md` を YAML frontmatter（`name: verify-<app>` と、アプリ・surface・いつ使うかを名指しする `description`。frontmatter がなければ skill は決して登録されない）付きで書き、次のセクションを、interview で実際に見つかったことに根ざして書く（プレースホルダーを残さない）。

- **Launch:** 検証のためにアプリを起動する正確なコマンド、そして準備ができたと分かる方法（log 行、応答するポート、prompt）。teardown も含める。短命な CLI や TUI では、生き続ける server はない。launch とは、バイナリをビルドする（または依存関係をインストールする）ことを一度行い、各 drive をそれぞれ独立した PTY か tmux セッションで開始することを意味する。
- **Doctor:** 「このインスタンスは駆動する価値があるか」に答える read-only チェックを 1 つ。プロセスが up しているか、正しいバージョン/ビルドか、ポートを自分が所有しているか、auth が有効か。何かがおかしく見えるときは agent がまずこれを実行する。
- **Drive:** 例ではなく、このリポジトリの実際の selector やコマンドを使った harness のレシピ。座標やタブ順序より、安定したハンドル（ARIA ラベル、data 属性、prompt 文字列、route パス）を優先する。
- **Evidence:** 証拠として何を捕捉し、どこに置くか。証明の基準を述べる。内部の setter や test 専用の endpoint ではなく実際のユーザー path を実行する。アクションと結果として生じた状態の両方を捕捉し、最終画面だけでなくする。side effect（書き込まれたファイル、挿入された行、送られたメッセージ）を見えるものと一緒に検証する。mock を使うのは、production の境界がすでに外部システムを隔離している場合だけ。安全な path が dry-run やテストモードの場合は、その名前を信じるのではなく、実際に何がスキップされるかを観測して検証する。dry-run の一部は、それでも network に触れたりブラウザを開いたりする。
- **Cleanup:** run が作成したインスタンスをどう畳むか。プロセス名では絶対に kill しない。自分が起動したものだけを kill する。cleanup はインスタンスと scratch な状態を削除するが、証拠は決して削除しない。証明の成果物は teardown を生き延び、skill が名指しする場所に残る。
- **Helpers:** skill が同梱する script はすべて実行可能で、その呼び出し方が skill の本文に示されている。読者がリバースエンジニアリングしなければならない helper は helper ではない。

## 3. Seed the feature map

`.claude/skills/verify-<app>/features/README.md` と、識別できるユーザー向け feature ごとに 1 ファイル（route、コマンド、メニュー、ドキュメントから、まずは上位 3〜5 個を目安に）を作成する。[`references/feature-map-example/`](references/feature-map-example/) の形式に従い、README の index と feature ごとに 1 ファイルとする。各ファイルは、ユーザーの視点から、その feature が何か、どうたどり着くか、harness でどう操作するか、どんな観測可能な最終状態がそれが動くことを証明するかに答える。4 つの H2 は `Sub-features`、`How to get to it (user POV)`、`Driving it with <harness>`、`Gotchas` である。この map はリポジトリで維持される検証の情報源であり、他にも記載されている入り口があるのに、都合の良い 1 つの入り口だけを駆動する証明は不完全である。

## 4. Prove the generated skill before handing it over

生成された skill 自身の指示を、一度 end to end で実行する。launch、doctor、マップされた feature を 1 つ drive し（1 つで十分。map があるおかげで、以降の run が残りをカバーできる）、証拠を捕捉し、cleanup する。cleanup の後、その証拠が名指しされた場所に依然として存在することを確認する。証拠を食べてしまう cleanup はこのステップに失敗する。失敗したものは直し、失敗したイテレーションのたびに生成された cleanup も実行して、壊れた試行がプロセスやポートを取り残さないようにする。一度も実行されたことのない生成 skill は draft であって成果物ではない。

## 5. Hand over the maintenance loop

map はアプリが変わった瞬間に古くなり、古い map はないよりも悪い。もう存在しない path を次の agent に教えてしまうからである。生成された skill がどう正直であり続けるか、ユーザーに伝える。

- マップされた feature の入り口、selector、コマンドが変わるたびに、現在の default branch に対して step 4 を再実行する。生成された skill 自身の **Drive** セクションが最初に壊れるものである。
- 新しいユーザー向け feature が出荷されたら feature file を追加し、feature が削除されたら削除する。マップされていない feature は検証されていない feature である。
- harness 自身の理由で失敗する drive（動いた selector、rename された flag）は、目の前のタスクで回避するのではなく、verification skill 自体のバグとして扱い、独立した PR で直す。

cadence は聞かれたときだけ提案する。
