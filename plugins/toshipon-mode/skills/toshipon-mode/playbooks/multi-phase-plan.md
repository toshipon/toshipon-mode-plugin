### Multi-phase or multi-PR plan

**プランはあなたが持つものであり、コードではない。プランは owner が箱ごとに実行し、operator が証拠から監査するチェックリストである。** phase や積み重ねた PR にまたがる作業向け。プランそのものが成果物である。実装しない。

1. 変更が 1、2 ファイルでアプローチが明白な場合は、プランをスキップする。そう述べて止まる。
2. 書く前に、prototype で未解決の問いを片付ける。レイアウト・タイミング・挙動・API が動くかどうかについての問いには `playbooks/prototype.md` を実行する。ブランチ、SHA、スクリーンショットを Appendix A 用に保存する。実験では決着しないプロダクトや好みの判断についてのみ operator に聞く。選択肢を示す（[never-block-on-the-human](../references/principles/never-block-on-the-human.md)）。
3. Agent tool、`subagent_type: "Explore"`、Delegation セクションに従った明示的な model で探索する（[guard-the-context-window](../references/principles/guard-the-context-window.md)）。それぞれがファイルへのポインタ、規約、テストコマンド、エントリポイントを返す。中身をそのまま貼り込まない。
4. 下の skeleton をプランファイルにコピーし、すべてのプレースホルダーを埋める。operator がパスを指定しない限り、リポジトリの `docs/` 配下に書く。見出しとサブブロックの順序をすべて示された通りに保つ。1 セクションが 1 PR。1 つの PR は自身の証拠を持つ 1 つの変更である（[sequence-verifiable-units](../references/principles/sequence-verifiable-units.md)）。**How to read this** で実行経路を名指しする。このセッション自身が一致する playbook の下で PR を駆動するか、多数の workspace にまたがる常設の複数日プログラムなら `superset:orchestrate` に渡す。
5. **technical-writing** skill に完全に従って書き、その後 **unslop** をかける。本文は 1 つの Diátaxis モード、how-to である。Appendix には説明と参照を置く。2 つのルールを逐語で適用する。「抽象的なメタファーは使わない」「ヘミングウェイのように書く」。各見出しはタスクか発見を述べる。long dash は禁止。文中のコロンも禁止。
6. 書き終えたプランを skeleton と照合し、見つけた逸脱をすべて直す（[encode-lessons-in-structure](../references/principles/encode-lessons-in-structure.md)）。skeleton のすべての見出しとサブブロックが揃って順序通りにあるか、すべての verification block が verification rule を逐語で開いているか、すべての箱が具体的な証拠を名指ししているか、long dash や文中コロンが残っていないかを目視で確認する。3 つ目のプランで 3 回目もこのチェックを走らせているなら、代わりに script として書く。
7. 引き渡す。プランのパスと、フォーマットチェックで見つかったことを投稿し、止まる。実行はオペレーターの明示的な go を待って、プランが名指しする実行経路の下で始まる。

**Verification.** テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みとなる（[prove-it-works](../references/principles/prove-it-works.md)）。この文が verification rule である。すべての verification block はこの文で始まる。live block は必須である。PR head で `model: "sonnet"` の少なくとも 3 つの並列レーンが、driver skill を通して本物の surface を駆動する。各レーンは 1 つの箱で、具体的なシナリオ、保存するスクリーンショット、合格の述語を持つ。perf block は指標、probe、最初に測定した trunk の baseline、失敗する数値を伴うルールを名指しする。インタラクションを変更する PR は review-gated である。operator が merge 前にチャットでスクリーンショットとビデオとともにレビューする。インタラクションを変更しない PR は `**Review gate.** None. <PR id> is not review-gated.` と書き、その下に箱を置かない。

**Driver skill.** surface で選ぶ。ブラウザと web UI は `claude-in-chrome` か `superset:browser` を使う。CLI・server・desktop app は組み込みの `run` skill を使う。iOS simulator の UI は `e2e-replay` を使う。生成済みの `verify-<app>` skill を持つリポジトリは、起動・doctor・drive・証拠経路をすでに知っているのでまずそれを使う。2 つの surface に触れる PR は両方にレーンを持つ。driver skill のない surface は Appendix C のリスクとし、その live block でも各レーンがどう駆動するかを名指しする。

````markdown
# <Program> plan

<10 行未満。何が変わるか、誰のためか、プログラムが強制するルール、順番通りの PR id。>

## How to read this

1 つの箱は 1 つの作業単位である。すべての箱がそれを確認する証拠を名指しする。ネストした箱は、その上の箱のサブステップである。箱をチェックするのは、その証拠、ファイル、ログ行、スクリーンショット、テスト実行、SHA が存在するときだけである。本文は how-to である。付録は説明と記録を行う。

プログラムは `<execution path>` で走る。<誰がマージするか、そして operator の項目のうちどの PR id が merge-ready で止まるか。>

テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みとなる。

## Program checklist

### Arm the program

- [ ] operator にこの protocol とこのプランを伝え、止まる。実行は operator の明示的な go があってから始める。
- [ ] go が出たら、この正確なテキストでセッション内の standing goal を再宣言する。「<プランのパス、順番通りの PR id、verification rule、誰がマージするか、done の条件。>」
- [ ] プログラム開始時にこれらを trunk から読む。tick ごとに再読する。
  - [ ] `cat ${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/SKILL.md`
  - [ ] `cat ${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/playbooks/<each playbook the program uses>.md`
  - [ ] `git show origin/master:<driver skill path>`
  - [ ] `cat ${CLAUDE_PLUGIN_ROOT}/skills/toshipon-mode/references/principles/INDEX.md`
- [ ] Claude Code 組み込みの `/loop` で 30 分ごとの audit tick を仕込む。cadence を記憶任せにしない。
- [ ] この tick プロンプトを逐語で使う。「trunk から execution playbook と standing goal を再読する。両方に対して operation を監査し、この tick で drift を直す。すべてのアクティブなレーンを probe し、side effect だけで進捗を判断する。行き詰まったレーンは stand down させ、その代替をすぐに dispatch する。それから、以前の status message がまだ報告していない追跡対象の変化をこの監査で見つけたときだけ、チャットで operator に短い status message を送る。変化とは、PR のオープン、code-ready な head、ラウンドの開始や終了、判定、マージ、行き詰まった agent と取った対応、blocker の追加や解消、operator にしか下せない判断などである。そうした変化をすべて名指しし、それ以外は書かない。table、マージ済みの一覧、変わっていない blocker を繰り返さない。監査で何も見つからなければ、返信テキストなしでターンを終える。どちらの場合も、この tick の行を decision trail に記録する。その行は報告した項目か none を名指しする。」
- [ ] operator の hold または stand-down の指示があれば、すべての owner に zero-writes order を一斉に送る。

### Spawn owners

- [ ] PR ごとに、その playbook が名指しする完全なライフサイクルを持つ owner を 1 人 spawn する。
- [ ] この依存グラフに従う。dependent な作業は、その親がマージされてから始めるか、親のブランチを base にする。
  - [ ] <PR id> と <PR id> は独立していて最初に来る。どちらも `master` から branch する。
  - [ ] <PR id> は <PR id> の後。
- [ ] ファイル境界を守る。<PR id or class> は `<glob>` にのみ触れる。
- [ ] review gate を守る。<PR ids> はインタラクションを変更する。マージ前に operator がチャットでスクリーンショットとビデオとともにレビューするのを待つ。

### PR mechanics, for every PR

- [ ] まず trunk と同期する。`/sync-main` を実行する。
- [ ] `/pr-create`（`--draft=false` を付けた `gh pr create`）で PR を draft ではなく ready で開く。
- [ ] PR 向けの push の前に `/verification-loop` を一度実行する。hook を有効にしたまま push する。
- [ ] 各 commit の前に、diff の prose とコメントに `/unslop` を実行し、レビュー前に narrating comment がないか diff を読み直す。
- [ ] すべての review-bot と security-reviewer のコメントを `../references/review-bot-triage.md` に従って triage する。
- [ ] code-ready report と CI パスの前に、現在の trunk に rebase する。fix ラウンドではその merge base を保つ。再び rebase するのは、merge 準備時、trunk との `git merge-tree` で conflict が出たとき、trunk 側の変更に起因する CI failure が出たときだけである。

### Verdict and merge, for every PR

- [ ] code-ready の head SHA と、その後 patch を変える各 push で、並列の verification agent を fan out する。1 つは `/verification-loop` を走らせる gate レーン。live レーンは PR の **Verify, live** block から。perf レーンはその **Verify, perf** block から。それぞれ独自の焦点を持つ 2 つ以上の audit レーンが diff と証拠を読み、PR body を疑ってかかる。判定の前に、merge-ready report の証拠を自分で監査する。
- [ ] すべてのレーンが `PASS` のときだけ clean とする。findings は owner に戻す。レーンが note として記録した defect も含む。新しい head は新しい fan-out と新しい判定を得る。ただし、次の patch-id ルールの下で有効なままの結果は除く。
- [ ] 判定の後、現在の trunk に rebase する。`git patch-id` は変わらない。patch-id が変わったら新しい head であり、新しい判定が必要である。

### Boot recipe, for every live lane

各 live レーンは PR head の自分自身の git worktree で動く。このプランが名指しする driver skill を通して駆動する。

- [ ] `git fetch origin <head-branch> && git worktree add <path> <head SHA>`。
- [ ] <backend と surface を起動する。ready になるのを待つ。>
- [ ] <入力は driver skill のコマンドを通してのみ届ける。read-only な診断手段を名指しする。>
- [ ] すべてのスクリーンショットを session scratchpad の `<scratchpad>/verify-<pr-id>/lane-<n>/<slug>.png` に保存し、report とともにパスを返す。

## <Task as a verb phrase> (<PR id>)

**Depends on.** <PR id, or None.>

**Files.**

- [ ] `<path>` を編集する。
- [ ] `<path>` を作成する。
- [ ] `<path>` を削除する。

**Build.**

- [ ] <1 つの変更。symbol とファイルを名指しする。>

**You see.**

- [ ] <1 つの観測可能な結果。正確な log 行や画面状態とともに。>

**Verify, unit.** テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みとなる。

- [ ] <test file と、それが得るケース。> `<command>` を実行する。

**Verify, live.** テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みとなる。boot recipe に従い、PR head で `model: "sonnet"` の少なくとも 3 レーン。

- [ ] Lane 1. <シナリオ。> `<slug>.png` を保存する。<述語> のとき合格。
- [ ] Lane 2. <シナリオ。> `<slug>.png` を保存する。<述語> のとき合格。
- [ ] Lane 3. <シナリオ。> `<slug>.png` を保存する。<述語> のとき合格。

**Verify, perf.** テストだけでは検証として不十分である。PR は unit、live、perf の箱がすべてチェックされて初めて検証済みとなる。

- [ ] Metric. <何を測定するか。>
- [ ] Probe. <コマンドまたは手順。trunk と head の両方で、交互に実行。>
- [ ] Baseline. 最初に trunk の <value> を記録する。
- [ ] Rule. <trunk に対する head。失敗する数値とともに。>

**Review gate.** operator がマージ前にレビューする。

- [ ] レーン <n> のスクリーンショットを `<media path>/<pr-id>-review-<slug>.png` にコピーする。
- [ ] レーンの worktree で変更内容の 30〜60 秒のビデオを録画する。`<media path>/<pr-id>-review.mp4` として保存する。
- [ ] スクリーンショットとビデオをチャットに投稿する。merge-ready で止まる。operator の go を待つ。

**Merge.**

- [ ] 正確な head SHA で clean な判定。
- [ ] review-bot triage 済み。
- [ ] 判定の後、現在の trunk に rebase済み。patch-id は変わらない。
- [ ] <誰が、どのコマンドでマージするか。>

## Close the program

- [ ] 上のすべての箱が、その証拠とともにチェックされている。
- [ ] `CLAUDE.md` に従った session-status block を添えて、report を operator に返信する。

## Appendix A. Prototype evidence

<prototype が答えた各未解決の問い。branch、SHA、成果物へのリンクとともに。未証明のまま残る各問い。>

## Appendix B. Alternatives rejected

<検討した各アプローチと、それが選ばれなかった理由。>

## Appendix C. Risks

<各リスクと、それが着地する PR、owner が見張るもの。>

## Appendix D. Links and reading list

<編集前に読むべきドキュメント。どの PR が `how` skill と `interrogate` skill を使うか。`show-me-your-work` skill に従った trail。>
````

**Reply:** プランのパス、依存関係と review-gated set を伴う PR id、prototype が証明したことと未証明のまま残ったこと、フォーマットチェックで見つかったこと。
