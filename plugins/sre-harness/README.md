# sre-harness

GitHub のレビュー履歴と仕様文書を読み、改善候補と影響範囲の報告を作る、読み取り専用の harness です。次の 3 つを同じ runtime で動かします。

- Review Learning: 指定期間の PR、レビュー、返信、解決状態、修正前後の差分から、指摘を構造化し、Rule や Skill の候補を作って評価します。
- Specification Consistency: 決定と要求の台帳を作り、default branch と open PR にまたがる未反映、矛盾、古い決定への依存を報告します。
- IaC Delivery: 状態遷移と証跡の契約だけを定義します。apply やデプロイは行いません。

この版（0.1.0）が持つのは、足場と安全境界と、Review Learning の全段（収集 collect、正規化 normalize、分割 split、分析 analyze、分類 classify、生成 generate、評価 evaluate、承認 approve / reject / retire）です。Specification Consistency と IaC Delivery は後続の変更で足します。

## インストール

Claude Code のセッション内で実行します。

```
/plugin marketplace add toshipon/toshipon-mode-plugin
/plugin install sre-harness@toshipon-tools
```

## 制約

この harness が行わないことを先に書きます。

- GitHub への書き込み、push、merge、クラウドの変更は行いません。REST は GET だけ、GraphQL は同梱した query だけ、git は harness 専用の bare リポジトリへの fetch と object の読み取りだけです。
- 利用者の checkout、git の設定、credential helper、`gh auth login` の認証情報は使いません。
- 実行データと生成物をリポジトリの中に書きません。保存先が git の作業ツリーの中にあると、exit 3 で止まります。
- 修正候補は Unified Diff のファイルとして出すだけで、適用しません。
- 候補の承認は人が行います。

読み取り専用は、許可リストとテストで担保しています。

| 層 | 内容 | 検査 |
|---|---|---|
| トークン | 本来の境界です。対象リポジトリに限定した fine-grained PAT（Contents: read、Pull requests: read、Metadata: read）を推奨します。書き込めるトークンを渡した場合、防御は下の層だけになります | 利用者の責任 |
| 子プロセス | プロセスを起動できるのは `runtime/sre_harness/guard.py` だけです | `tests/invariants/test_boundaries.py` が AST で検査 |
| REST | `gh api --method GET` を固定の argv で呼びます。endpoint は `guard.REST_ENDPOINTS` の雛形だけです | `tests/unit/test_guard.py` の表 |
| GraphQL | `runtime/sre_harness/github/queries/*.graphql` を POST します。query を入力から組み立てません。server が返した cursor は変数として渡し、guard が形式を検査します | 同上 |
| git | `guard.GIT_SUBCOMMANDS` にあるサブコマンドと flag だけです。`apply` は `--cached --check` が必須です | 同上 |
| Claude CLI | `guard.claude` が固定の argv で呼びます（`-p --output-format json --json-schema … --bare`（api-key）か `--safe-mode`（subscription）、`--tools ""`、`--disable-slash-commands`、`--strict-mcp-config`、空の `--mcp-config`、`--no-session-persistence`、`--max-budget-usd`）。権限を迂回する flag は付けません。cwd は空の一時ディレクトリ、prompt は stdin です | 同上 |

## Review Learning: 収集と正規化

段ごとの入力・出力・失敗・権限は `skills/review-learning/SKILL.md` の表にあります。

```bash
export REVIEW_READ_TOKEN=...   # 読み取り専用の fine-grained PAT
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" review collect \
  --repo example-org/demo --since 2026-01-01T00:00:00Z --until 2026-03-31T23:59:59Z \
  --auth-env REVIEW_READ_TOKEN
# => {"run_id": "run-...", "status": "complete", ...}
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" review normalize --run run-...
```

- `collect` は PR を更新日時の新しい順に読み（`pulls?state=all&sort=updated&direction=desc`）、`--since` より古い PR が現れたページで止めます。Search API は使いません。`--until` より新しい PR は読み飛ばします。
- ページ送りは 1 ページずつです。`Link` header からは `rel="next"` と `rel="last"` の page 番号だけを取り出し、次のリクエストは許可リストの雛形から組み立てます。サーバーが返した URL を `gh` に渡しません。
- rate limit: `X-RateLimit-Remaining: 0` なら `X-RateLimit-Reset` まで、403 / 429 の secondary limit は `retry-after`（無ければ 60 秒）だけ待ちます。待機の合計が `--max-wait`（既定 900 秒）を超えると、待たずに止めて `partial` にします。
- 途中のページが 404 などで読めないときは `collect.json` の `missing` に記録して続け（先に `rel="last"` が分かっていれば後続のページも読みます）、最後に `partial`、exit 1 で終わります。1 応答が 2 MiB を超えたら切り詰めずに止めます。
- `--fixture <dir>` は合成応答を読み、ネットワークも `gh` も使いません。`--auth-env` とは同時に指定できません。合成応答は `runtime/examples/synthetic/github_responses.py` が作ります（`cd runtime && python -m examples.synthetic.github_responses <出力先>`）。
- `normalize` は `raw/` から `threads.jsonl`（`runtime/schemas/review-thread.schema.json`）と `rejected.jsonl` を作ります。kind は `inline_thread`、`review_summary`、`issue_comment` です。REST と GraphQL の両方から来た同じコメントや、ページ境界で重複したコメントは `node_id` で 1 件にします。削除済みユーザーは `author_type: ghost` です。取得元が返さなかったフィールド（`isResolved`、fork 削除で null になった `head.repo` など）は既定値で埋めず、`missing` に名前を並べます。

### 収集するデータと送信先

| 送信先 | 送るもの | 受け取って保存するもの |
|---|---|---|
| GitHub REST（`gh api --method GET -i`） | リポジトリ名、PR 番号、page 番号、トークン（`GH_TOKEN` として子プロセスの環境変数で） | PR の一覧（番号、状態、作成者、日時、base / head の SHA、head のリポジトリ名）、レビュー、review comment、PR の会話コメント、commit の一覧、変更ファイルと patch |
| GitHub GraphQL（同梱の `review_threads.graphql`、`thread_comments.graphql` を POST） | 同上と cursor | review thread の解決状態、outdated、解決者、path と行、コメントの返信先・commit・投稿者の種別、`rateLimit` |

保存するのは応答の本文と、`Link`、`X-RateLimit-*`、`Retry-After`、`Content-Type` の header だけです。run ディレクトリにはレビュー本文と patch が入ります。LLM にはこの段では何も送りません。

## Review Learning: 分析と分類

```bash
# 合成データや動作確認: 決定論的な stub。何も送らず、費用も認証も要りません
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" review analyze --run run-... --backend stub
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" review classify --run run-... --backend stub \
  --existing-rules path/to/rules

# Claude を使う: PR 本文・コメント・diff が Anthropic に送られます（下の表）
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" review analyze --run run-... --backend claude \
  --auth-mode subscription --max-run-budget-usd 2 --deny-terms-file ~/.config/ai-harness/deny-terms.txt
```

- `--backend` に既定値はありません。`claude` を選ぶと `--auth-mode api-key|subscription` と `--max-run-budget-usd` も必須です。api-key では環境変数 `ANTHROPIC_API_KEY` だけを、subscription では `HOME` だけを子プロセスに渡します。
- **stub は LLM の品質を何も示しません。** stub は固定の規則（最初のコメントの 1 文目で thread をまとめる、「on purpose」を含む返信を例外にする、「instead of X」を X の方針への反対とみなす、など）で答えを作ります。stub での E2E が証明するのは、配管と決定論的な検査（引用、ID、時刻、信頼度、別枠、矛盾候補）が動くことだけです。
- `analyze` は最初に、inline thread ごとに修正 commit を探して `fix` を付けます（`threads-with-fix.jsonl`）。使うのは collect が保存した commit 一覧と変更ファイルだけで、通信しません。方法は suggestion の適用、返信に書かれた commit の SHA、同じ path を変えた後の commit の順に試し、候補が複数なら `ambiguous` のまま残します。force push で元の commit に到達できない thread は `not_found` で、`missing` に `fix.before` が入ります。
- backend が出した lesson は、JSON Schema に加えて次の検査を通ったものだけを `lessons.jsonl` に入れます。引用（`quotes[].text`）が該当コメント本文の部分文字列であること、参照する thread と comment が送った DATA に存在すること。通らなかった lesson は理由付きで `rejected-lessons.jsonl` に入り、全件が通らなければ exit 1 です。
- 次の値は backend の自己申告ではなく、harness が規則で決めます。
  - `confidence`: `confidence_basis`（thread 数、レビュアー数、PR 数、反対する thread 数）から `review/analyze.py` の `CONFIDENCE_RULES` で決めます。3 thread 以上・3 PR 以上・3 人以上で反対がなければ `strong`、3 thread 以上・2 PR 以上・2 人以上なら `recurring`、それ以外は `candidate` です。
  - `source_trust`: lesson を支える thread の最初のコメントの投稿者です。owner / member / collaborator の人がいれば `member_supported`、bot だけなら `bot_only`、それ以外（外部、ghost、association 不明）は `external_only` です。
  - `reviewer_agreement`: 解決済みか修正が見つかった thread は agreed、返信があるのに未解決・未修正なら disputed、返信がなければ unanswered。1 つでも disputed があれば lesson は disputed です。
  - `staleness`: backend が「この thread はこの lesson の方針を覆す」と候補を出し、その thread の時刻が lesson のどの thread よりも後なら `superseded` とし、`later_evidence` に thread を残します。時刻が前なら覆さず、`analyze.json` の `ignored_oppositions` に `not_later` と記録します。
- `classify` は、superseded、`case_specific`、`external_only` / `bot_only` の lesson を候補にせず `set-aside.jsonl` に理由付きで出します。残りは `review/classify.py` の `TARGET_RULES`（固定の表）で rule / skill / policy_as_code / evaluation / knowledge / regression_test のどれかに決めます。
- `--existing-rules` の Markdown は読むだけです。symlink は辿らず（`classify.json` の `skipped_rule_files` に出ます）、1 ファイル 256 KiB、合計 512 KiB、500 ファイルを超えると切り詰めずに exit 2 で止まります。backend が出した既存ルールとの関係（contradicts / duplicates / narrows）は、引用がその行の部分文字列のものだけを `conflicts.jsonl` に残します。これは承認者に見せる注意書きで、分類も exit code も変えません。

### Claude に送るデータ

| 送信先 | 段 | 送るもの |
|---|---|---|
| Anthropic（Claude CLI 経由） | `analyze --backend claude` | リポジトリ名、PR 番号と本文、thread のコメント本文・投稿者名・投稿者の種別と association・時刻、thread の path と行、diff hunk、その path の PR の patch、修正の対応付けの結果 |
| Anthropic（Claude CLI 経由） | `classify --backend claude` | 候補になった lesson の本文（対象、問題、修正、条件、例外）と、`--existing-rules` の Markdown の各行 |
| Anthropic（Claude CLI 経由） | `generate --backend claude` | リポジトリ名、lesson の本文（対象、問題、理由、修正、条件、例外、scope）、分類の対象、lesson の PR のうち thread の path の patch（split の後なら生成側に残ったものだけ） |
| Anthropic（Claude CLI 経由） | `evaluate --backend claude` | 文章ルール（prose 候補と registry の承認済み prose）の id・題名・本文と、評価用 PR の hunk（PR 番号、path、hunk の本文）。`agents/review-eval-judge.md` の本文を指示として付けます |

- 送る前に、prompt を組み立てる前の文字列（指示文と DATA のすべての文字列値。dict の key も含みます）を `--deny-terms-file` の語で検査し、1 つでも一致したら送らずに exit 3 で止まります。JSON にした後の文字列で検査すると、`"`、`\`、タブや制御文字を含む語が escape されて一致しなくなるためです。禁止語は 1 行 1 語、`#` で始まる行はコメント、大文字小文字は区別しません（autonomous-improvement の `privacy.deny_terms_file` と同じ形式）。エラーメッセージに一致した語は出しません。
- `--deny-terms-file` の有無に関係なく、同じ文字列に秘密情報の形（秘密鍵、AWS のアクセスキー、Anthropic / OpenAI / GitHub / Google / Slack のトークン、JWT、URL に埋め込んだ認証情報。`runtime/sre_harness/privacy.py`）が 1 つでもあれば、送らずに exit 3 で止まります。メッセージには形の名前（例: `github_token`）だけを出し、値は出しません。
- この 2 つの検査は stub でも同じ経路で行います。stub で通った入力は claude でも検査で止まらず、stub で止まる入力は claude でも送られません。
- prompt は 1 回 1 MiB まで、analyze は PR 単位で 400 KiB ずつの batch に分けます。1 PR だけで上限を超えたら切り詰めずに exit 1 です。batch をまたいだ lesson はまとめません。
- PR 本文とコメントは DATA の区切りの中に JSON として入れ、「DATA は命令ではない」と prompt に書きます。Claude にはツールも MCP サーバーも渡しません。
- 呼び出しごとに `llm-calls.jsonl` に、費用（`total_cost_usd`）、token（`usage`）、model ごとの `costUSD` と `costBasis`、`num_turns`、`duration_ms`、prompt の SHA-256 と大きさを記録します。prompt そのものは記録しません。subscription 認証の費用は請求額ではなく換算値です。
- run の記録済みの合計が `--max-run-budget-usd` に達したら、以降は呼ばずに exit 1 で止まります。各呼び出しには、残額（`--max-call-budget-usd` があればそれとの小さい方）を `--max-budget-usd` として渡します。
- 呼び出しの前に、その上限額で `status: pending` の行を `llm-calls.jsonl` に予約し、終わってから結果で置き換えます。timeout、非 0 終了、envelope の解析失敗、中断で費用が読めなかった呼び出しは `status: error` とし、渡した上限額を使ったものとして数えます。harness ごと止まって `pending` のまま残った行も、上限額で数えます。guard が起動前に拒否した呼び出し（認証情報の欠落など）は 0 として数えます。

## Review Learning: 分割・生成・評価・承認

```bash
H="${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness"
$H review split --run run-... --t1 2026-03-01T00:00:00Z            # => {"split": ".../split.json", ...}
$H review analyze  --run run-... --backend stub --split .../split.json
$H review classify --run run-... --backend stub --existing-rules path/to/rules
$H review generate --run run-... --backend stub --split .../split.json
$H review evaluate --run run-... --backend stub --split .../split.json
# 承認は人が端末で行う（下の「承認の制約」）
$H review approve --run run-... --candidate IC-0001 --reviewer alice --reason "..." \
  --activate-as docs/rules/http-timeout.md
```

### 分割（split）

- 分割は PR の merge 日ではなく出来事の時刻で行います。生成側は `--t1` より前に作られた PR で、そのうち `t1` より前のコメントと commit だけを使います。`t1` 以降（または時刻のない）コメントと commit は使わず、件数を `split.json` の `generation.dropped` と `analyze.json` の `split_left_out` に残します。`t1` 以降の commit がある PR は patch も使いません（patch は PR の最終状態で、その commit を含むことがあるためです）。最初のコメントが `t1` 以降の thread は丸ごと使いません。
- 評価側は `created_at` が `t1 + --gap-days`（既定 0 日）以降の PR です。間の PR と作成時刻のない PR はどちらにも入りません。
- `split.json` は期間、PR 番号、`sha256`（`created_at` と `sha256` を除いた全項目の hash）を持ちます。内容と hash が合わない split、別リポジトリの split は exit 2 です。
- analyze と generate に同じ `--split` を渡します。evaluate は、analyze か generate がこの split で動いていないとき、または `lessons.jsonl` が評価用 PR を 1 件でも参照しているとき、exit 2 で止まります（autonomous-improvement のホールドアウトと同じ規則です）。

### 生成（generate）

- 候補は `classified.jsonl` の lesson からだけ作ります。`set-aside.jsonl` の lesson（superseded、case_specific、external_only、bot_only）からは作りません。`--include-untrusted` を付けたときだけ、external_only / bot_only だけを理由に外された lesson からも作り、候補に `untrusted_included: true` を付けます。classify の出力とは別に、generate 自身も lesson の `source_trust` を見て、flag なしでは external_only / bot_only の lesson を飛ばします。
- 候補は `candidates/<IC-id>/candidate.json`（`runtime/schemas/improvement-candidate.schema.json`）と、承認の対象になる本文 `body.md` です。ファイル名は候補の id と固定の名前だけで、入力に含まれる path は使いません。`artifact.content_sha256` は `body.md` の SHA-256 です。**正本は hash が覆う `body.md` だけです。** evaluate が当てる検査と form、承認画面の「記録される内容」、registry に入る title・form・check は、どれも `body.md` を読み直して parse したものから作ります。`candidate.json` の `form`、`title`、`check` が parse の結果と違えば、evaluate も approve も exit 3 で止まります。`candidate.json` のそれ以外（target、sources、lint、risk など）は判断の材料で、hash は覆わず、registry にも入りません。
- `form` は `declarative_check` か `prose` です。`declarative_check` の検査は `body.md` の末尾の ```` ```json sre-harness-check ```` ブロックにあるデータ（`path_globs`、`added_line_pattern`、`absent_pattern`、`message`）で、harness の解釈器（`runtime/sre_harness/review/apply_rules.py`）が評価します。生成されたコードは実行しません。`prose` は文章のルールで、評価では `review-eval-judge` が当てます。
- `lint` は本文に agent 向けの命令文、URL、shell コマンド、秘密情報らしき文字列があれば、種類と位置（`span`）を記録します。拒否ではなく承認者への注意書きで、status は変えません。
- `risk`、`rollback`、元になったコメント（`sources`：引用と投稿者の種別・association）、classify が見つけた既存ルールとの関係（`conflicts`）を持ちます。autonomous-improvement の提案（差分、測定結果、リスク、ロールバック）と同じ要素です。
- **stub は LLM の品質を示しません。** stub は「... no X.」という lesson で、修正が呼び出しに X を足していれば、その呼び出しで X のない追加行を指摘する検査を作り、それ以外は lesson の文をそのまま並べた prose にします。

### 宣言的検査の解釈器

- hunk の path が `path_globs` のどれかに一致し（`**` はディレクトリをまたぎ、`*` と `?` はまたがない）、追加行のどれかが `added_line_pattern` に一致し、`absent_pattern` があるときは hunk の新しい側（追加行と文脈行）のどこにも一致しないとき、指摘を出します。
- 正規表現は 200 文字まで、glob は相対 path だけです。照合は `guard.regex_search` が同梱の `runtime/sre_harness/regex_worker.py` を `python -I` で別プロセスとして起動し、pattern と行を標準入力の JSON で渡して行います（argv は固定）。既定 5 秒で打ち切り、その候補だけ `check_failed`（`regex_timeout`）にして評価は続けます。4,000 文字を超える行は照合せず件数を数えます。

### 評価（evaluate）

- 単位は「hunk × 指摘の種類（ルール）」です。評価用 PR の修正前のコードを使います。thread が付いた hunk は thread の `diff_hunk`（レビュアーが見た時点の hunk）、thread のないファイルは PR の patch の hunk です。
- ルールは registry の承認済み（このリポジトリ、最新版が approved）を「既存ルール」とし、この run の候補を足して比べます。`with_candidates`（既存 + 全候補）、`baseline`（既存だけ）、候補ごと（既存 + その候補）の 3 種を出します。既存ルールの Markdown（`--existing-rules`）は実行できる形でないので、比較の基準には入りません。
- ラベル: thread の最初のコメントがルールの title（`body.md` の見出し）と語を共有する（共有語が min(3, n) 以上かつ title の語の半分以上）とき、その thread はその種類の指摘とみなします。修正が見つかれば `human_flagged`、`fix.status = ambiguous` なら `ambiguous`（正解から外す）、修正がなければ `disputed`（指標から外し件数を出す）、指摘がなく adjudication が valid なら `human_missed_valid`、それ以外は `clean` です。
- 指標: `precision_lower`（下限。人が確認していない指摘をすべて誤りと数える）、`precision_confirmed`（確認後。レビュアーが指摘したものと adjudication 済みのものだけ）、`recall`、`f1`（下限の precision と recall から）、`false_positive_rate`（clean なセルに出た指摘 / clean なセル数）。分母が 0 の指標は数値を出さず `undefined` です。
- 95% 区間は評価用 PR を単位にした bootstrap（同じ PR の hunk は独立でないため）で、seed（既定 20260127）と回数（既定 1,000）を `evaluation.json` の `criteria.thresholds` に記録します。n（評価用 PR の数）を併記します。
- **評価用 PR が 30 件未満の指標は数値ではなく `unevaluable` です。** `status: unevaluable` と理由を記録し、exit 0 で終わります。件数（`counts`）は出すので、どの hunk で何が起きたかは読めます。
- `criteria_hash` は split の hash、指標の定義、閾値（30 件、95%、seed、回数）、ラベルの規則の hash です。`--seed` や `--resamples` を変えると別の評価になります。adjudication は基準ではなく正解の補正なので、`adjudication_sha256` として別に記録します。
- 工数は review の各段の wall-clock（`timings.jsonl`）、コストは `llm-calls.jsonl` の集計で、`costBasis` を併記します。subscription 認証の費用は請求額ではありません。
- `evaluation.md` に載せる題名や path は escape し、リンクと画像の記法を通しません。
- 本文 `body.md` を人が編集した場合、次の evaluate がそれを新しい版（`version` + 1、status は proposed から evaluated）として評価します。検査ブロックが読めない本文は `check_failed` になり、承認できません。

adjudication（`--adjudication <file>`、schema `review-adjudication`）は、`evaluation.json` の `findings` を見た人が書きます。

```json
{
  "schema_version": 1,
  "split_sha256": "<split.json の sha256>",
  "entries": [
    { "hunk_sha256": "<findings[].hunk_sha256>", "kind": "IC-0001", "verdict": "valid", "reviewer": "alice" },
    { "hunk_sha256": "<...>", "kind": "IC-0001", "verdict": "invalid", "reviewer": "alice", "note": "test code" }
  ]
}
```

### 評価の読み方

- 下限と確認後の precision の差は、人が確認していない指摘の多さです。人のレビューを正解と決め付けないため、両方を出します。
- `unevaluable` のときは数値を作らない、補わないでください。
- **合成データ（`runtime/examples/synthetic/`）での数値は機構の検証であり、精度の主張ではありません。** テストは「過去指摘の再現」「正常例の誤検知」と、30 件以上を合成したときに数値が出ることを確かめているだけです。
- stub での評価は、解釈器・ラベル・指標・状態遷移が動くことを示すだけで、LLM の品質を示しません。

### autonomous-improvement の評価器として使う

`--format evaluator-output` は、autonomous-improvement の `schemas/evaluation.schema.json#/$defs/output` に合う JSON（`metrics`、`evidence`、`unevaluable`、`notes`）を標準出力に出します。件数不足のときは `metrics` を空にし、`unevaluable` に理由を入れます。接頭辞のない指標は既存 + 候補、`baseline_` は既存だけです。分母が 0 の指標は出さず、`notes` に名前を書きます。v2 の設定例（`evaluation.command`）:

```json
{
  "command": [
    "/path/to/plugins/sre-harness/scripts/sre-harness", "review", "evaluate",
    "--run", "run-20260301T000000Z-0123abcd", "--split", "/path/to/split.json",
    "--backend", "stub", "--format", "evaluator-output"
  ],
  "metrics": [
    { "name": "precision_lower", "kind": "deterministic", "role": "scored", "direction": "higher" },
    { "name": "recall", "kind": "deterministic", "role": "scored", "direction": "higher" }
  ]
}
```

`--backend claude` で prose 候補を評価する場合は、指標が LLM の判定を含むので v2 側では `kind: "llm"` にします（各指標に `rationale` を付けています）。

### 承認の制約

- `review approve` は stdin と stdout の両方が TTY のときだけ動きます。TTY がなければ何も記録せず exit 3 です。この検査を迂回する flag や環境変数はありません。agent の shell には TTY がないので、agent は承認できません。**これは本人確認ではありません。** 端末の前にいる人が誰かを確かめる仕組みではなく、`--reviewer` は入力どおりに記録します。
- 承認の画面には、まず registry に記録される内容そのもの（`body.md` から parse した form、title、check）を出し、続けて本文、本文の lint、元になったコメントの引用（コードブロック）と投稿者の種別・association、既存ルールとの関係、評価の結果、リスクとロールバックを出します。外部の文字列は escape し、制御文字は置き換えます。
- 画面の後で `content_sha256` の先頭 12 文字を入力します。違えば exit 3 です。候補が同じ `content_sha256` で評価されていない（未評価、評価後に本文が変わった、`check_failed`）ときも exit 3 です。
- 記録は state root の下の `registry/<RG-id>/v<N>.json`（schema `review-registry`）と、そのときの本文 `v<N>.md` です。記録する値は、`body.md` から parse した値と hash、run から決まる値（リポジトリ、run id、候補 id）、承認者の入力だけで、`candidate.json` の値は入れません。評価の後に `candidate.json` の `check` などを書き換えると、承認は exit 3 です。registry の記録と `v<N>.md` が食い違う（hash か parse 結果が違う）entry は、次の evaluate で基準として読むときに exit 3 で止まります。run とは別の場所です。版は追記だけで、既存のファイルは置き換えません。承認後に本文を変えて評価し直し、もう一度承認すると版 2 になります。`review reject` と `review retire` も版として記録します。
- 有効化は、registry の記録と、`--activate-as <path>` を付けたときの Unified Diff（`v<N>.diff`。指定した path に本文を足す新規ファイルの差分）を出すまでです。作業ツリーには書かず、commit もしません。path は相対 path だけで、`..`、絶対 path、`.git/`、`.github/` は exit 3 です。diff を適用して commit するのは人です。
- 承認済みのルールは、同じリポジトリの次の evaluate で「既存ルール」になります。

## 必要なもの

- Python 3.10 以上と `jsonschema`（`runtime/requirements.txt`）
- GitHub を読む段では `gh` と `git`

plugin のディレクトリは変更しません。venv は別の場所に作ってください。

```bash
python3 -m venv ~/.venvs/sre-harness
~/.venvs/sre-harness/bin/pip install -r "${CLAUDE_PLUGIN_ROOT}/runtime/requirements.txt"
export SRE_HARNESS_PYTHON=~/.venvs/sre-harness/bin/python
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" runs list
```

## 認証

GitHub を読む段は `--auth-env <環境変数名>` が必須で、既定値はありません。その変数が空なら、通信せずに exit 2 で止まります。

```bash
export REVIEW_READ_TOKEN=...   # 読み取り専用の fine-grained PAT
```

渡した値は、`gh` には `GH_TOKEN` として、`git` には環境変数経由の設定として渡ります。argv、remote の URL、リポジトリの config、ログ、run ディレクトリには書きません。`gh` には空の設定ディレクトリを、`git` には空の global / system 設定を見せます。子プロセスの stderr は保存しません。

## データの保存場所

| 項目 | 場所 |
|---|---|
| ルート | `$AI_HARNESS_HOME/sre-harness/`。未設定なら `$XDG_STATE_HOME/ai-harness/sre-harness/`、それも無ければ `~/.local/state/ai-harness/sre-harness/` |
| run | `<ルート>/runs/<run_id>/`。毎回新しいディレクトリで、既存の run は上書きしません |
| 権限 | ディレクトリ 0700、ファイル 0600 |

ルートの親（`ai-harness/`）は toshipon-mode plugin の autonomous-improvement と共有します。run にはレビュー本文と diff が入ります。消すときは `runs/<run_id>/` を削除してください。

ホームディレクトリを git で管理している場合、既定の場所は拒否されます。`AI_HARNESS_HOME` にリポジトリの外のディレクトリを指定してください。

## exit code

| code | 意味 |
|---|---|
| 0 | 成功 |
| 1 | 品質 gate の不合格、または結果が不完全 |
| 2 | 入力か設定の誤り |
| 3 | 安全境界による拒否 |

## 開発

```bash
cd plugins/sre-harness/runtime
python -m pip install -r requirements-dev.txt
ruff check . && ruff format --check . && pyright
python -m coverage run -m pytest && python -m coverage report
```

テストはネットワークも認証も使いません。例と fixture は合成データだけです。

## 未検証

- APM での配置。launcher は自分の位置から `runtime/` を解決します。APM が plugin を分解して置く構成では動かない可能性があります。Claude Code の `/plugin` での導入を対象にしています。
- git に環境変数経由でトークンを渡して private リポジトリを fetch できるか。合成データのテストはローカルの fetch だけを通ります。
- 実際の Claude CLI に対する `review analyze` / `review classify` / `review generate` / `review evaluate`。テストは `guard._run` を差し替えた経路と stub だけを通ります。`--bare`（api-key）の envelope の形と `costBasis` の値は実機で確かめていません。
- 実際の GitHub に対する `review collect`。テストは合成応答と、`gh` の起動を差し替えた経路だけを通ります。`gh api graphql -i` の出力形式、secondary limit の応答（403 / 429 と `retry-after`）、fine-grained PAT で `isResolved` を読めるかは実機で確かめていません。
- 実データでの precision / recall。合成データの数値は機構の検証で、どのルールの精度も主張しません。
- 承認の TTY 検査が、実際の端末と agent の shell で期待どおりに分かれること。テストは TTY のない経路（exit 3）と、TTY の判定だけを差し替えた経路を通ります。

## Specification Consistency: 取り込みと抽出

仕様に関わる文書を共通の形（schema `spec-document`）に正規化し（`spec ingest`）、そこから Decision Registry と Requirement Registry を作ります（`spec extract`）。台帳（`decisions.json`、`requirements.json`）が正本で、文書はその view です。トレースと open PR の分析（`spec analyze`）と報告（`spec report`）は下の「Specification Consistency: トレースと open PR の分析」にあります。段ごとの入力・出力・失敗は [spec-consistency の SKILL.md](skills/spec-consistency/SKILL.md) にあります。

```bash
# 取り込み: ファイルだけなら認証は要りません
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec ingest --manifest path/to/manifest.json
# manifest に GitHub の PR があるとき
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec ingest --manifest path/to/manifest.json --auth-env SPEC_READ_TOKEN
# => {"run_id": "run-...", "status": "complete" | "partial", "documents": N, "input_missing": N}

# 抽出: stub は何も送りません。claude は文書の section を Anthropic に送ります（下の表）
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec extract --run run-... --backend stub
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec extract --run run-... --backend claude \
  --auth-mode subscription --max-run-budget-usd 2 --deny-terms-file ~/.config/ai-harness/deny-terms.txt
```

合成データで試すときは `runtime/` で `python -m examples.synthetic.spec_scenarios <dir>` を実行すると、文書一式、`manifest.json`、GitHub の合成応答（`<dir>/github-fixture`、`--fixture` に渡す）ができます。

### manifest の書き方

```json
{
  "schema_version": 1,
  "files": [
    { "path": "docs/adr/0005-http-client-b.md", "kind": "adr" },
    { "path": "docs/requirements.md", "kind": "markdown" },
    { "path": "docs/minutes/2026-01-15.md", "kind": "minutes" },
    { "path": "exports/slack-sre.json", "kind": "slack_export" },
    { "path": "exports/confluence-runbook.json", "kind": "confluence_export", "title": "任意" }
  ],
  "github": [{ "repo": "example-org/demo", "pull": 12 }],
  "patch_globs": ["docs/**/*.md"]
}
```

- `path` は manifest のあるディレクトリ（基準ディレクトリ）からの相対 path です。絶対 path、`~` で始まる path、`..` を含む path、`\` を含む path は、何も読まずに exit 3 で拒否します。基準ディレクトリより上を指す方法はありません。
- symlink は辿りません。途中のディレクトリとファイル自体を `lstat` で確かめ、`O_NOFOLLOW` で開きます。symlink があれば exit 3 です。manifest 自体が symlink でも exit 3 です。
- ファイルは読むだけです。1 文書 2 MiB、run の合計 64 MiB を超えたら、切り詰めずに exit 1 で止まり、run を作りません。
- manifest にあるのに無いファイル、通常のファイルでないもの、UTF-8 でないもの、形の違う export は、既定値で埋めずに `ingest.json` の `input_missing` に理由（`file_not_found`、`not_a_regular_file`、`not_utf8`、`unreadable_export`）付きで記録します。読めた分は `documents.jsonl` に書き、run は `partial`、exit 1 です。
- `patch_globs` は後続の patch の段が使う「patch を出してよい文書」の glob です。この段では検査（`..`、絶対 path、`.git/`、`.github/` は exit 3）して `ingest.json` に保存するだけです。
- 出力ファイル名と文書 ID（`DOC-0001`）は harness が決めます。入力の path は `provenance.locator` に入るだけで、ファイル名には使いません。

### 文書の形

| kind | 読み方 |
|---|---|
| `markdown`、`adr`、`minutes` | 先頭の front-matter（`---` で囲む）と ATX 見出し（`#`〜`######`、code fence の中は除く）で section に分けます。front-matter は `key: value`、`key: [a, b]`、`key:` の下の `- item` だけを読み、入れ子の mapping、`|` / `>`、`{...}`、読めない行は `missing` に `front_matter.<key>` / `front_matter.line_<n>` として入れ、値は入れません。閉じていない front-matter は `missing` の `front_matter` になり、全体を本文として読みます |
| `slack_export` | Slack の export の日ごとのファイルの形です。JSON の配列で、各要素は `ts`（`1768435200.000100` の形の文字列）と `text`（文字列）を必ず持ち、`user_name` か `user` があれば見出しにします。`thread_ts` は locator に残します。API は呼びません |
| `confluence_export` | 保存したページの JSON で、`title` と `body.storage.value`（storage 形式の XHTML）を必ず持ちます。`h1`〜`h6` で section に分け、タグと属性（リンク先を含む）は捨てて本文だけを残します。API は呼びません |
| `github_pr` | PR 本文を Markdown として section に分けます。`provenance.sha` は head の SHA です。本文が無ければ `missing` に `body` |
| `github_issue_comment` | PR の会話コメントを 1 コメント 1 section にします（locator は `issue_comment <id>`）。review comment と thread は `review collect` の範囲です |

GitHub は `gh api --method GET` で `repos/{owner}/{repo}/pulls/{number}` と `repos/{owner}/{repo}/issues/{number}/comments` だけを読みます（`guard.REST_ENDPOINTS`）。`--auth-env` の変数が空なら通信せずに exit 2、manifest に GitHub の PR があるのに `--auth-env` も `--fixture` も無ければ exit 2 です。読めなかった PR やページは `input_missing` に HTTP の理由（`http_404` など）で記録します。

### 抽出の規則

- **明示（`basis: explicit`）は規則で取ります。** 決定は `kind: adr` の文書から取ります。front-matter の `id`、`title`、`status`、`supersedes`、`approvers`（`名前 (役割)` の形）、`date`、`systems`、`components`、`environments`、front-matter が無ければ本文の `Status:`、`Supersedes:`、`Date:` の行か `## Status` の section です。要求は全文書の `REQ-0001 ...` で始まる見出しの section から取ります。最初の段落が statement、`Given` / `When` / `Then` の行（箇条書きでもよい）と `Acceptance criteria:` / `受入条件:` の後の箇条書きが受入条件、`Category:`、`Decisions:`、`Rationale:` の行が残りの項目です。要求の ID は原文の `REQ-` の番号をそのまま使い、決定の ID（`DEC-0001`）は harness が振って原文の ADR 番号を `external_id` に残します。
- **状態は harness が決めます。** 原文の状態は `stated_status` に残し、`status` は次の規則で決めます（`runtime/sre_harness/spec/registry.py`）。approved（または superseded）の決定が `supersedes` に挙げた決定だけが superseded になります。却下・提案中・未解決の決定は何も置き換えません（`issues` に `cannot_supersede`）。approved と superseded には approver と根拠が要り、無ければ `unresolved`（`approval_incomplete`）です。superseded と書かれているのに置き換える approved の決定が無ければ `unresolved`（`no_approved_successor`）です。3 段の連鎖では末端だけが active です。置換の循環は exit 2 で止まり、何も書きません。存在しない決定への参照は `supersedes` / `decision_refs` から外し、`input_missing` に記録します。
- **推定（`basis: inferred`）だけを backend に頼みます。** backend は明示で取れなかった決定と要求、規則が見落とした曖昧さを提案します。根拠（`evidence[].ref` と `quote`）が送った section を指し、引用がその section の部分文字列であるものだけを残します。推定の決定は approved にできません（`inferred_cannot_be_approved` で除きます）。推定は `confidence: low` に固定し、backend の自己申告を使いません。除いたものは `rejected-extractions.jsonl` に理由付きで残ります。
- **stub は LLM の品質を何も示しません。** stub は議事録の `決定:` / `Decision:` 行を提案中の決定、`要求:` / `Requirement:` 行を要求とし、`TBD` を含む要求に曖昧さを 1 件付けるだけです。

### 曖昧さの検出

`runtime/sre_harness/spec/ambiguity.py` の表で検査し、`detector: rule` と記録します。backend が足した分は `detector: llm` で、`extract.json` の `ambiguity` で別に数えます。

| kind | 規則 |
|---|---|
| `vague_term` | `VAGUE_TERMS` の語（「適切に」「できるだけ」「高速」「as appropriate」「fast」など）が statement か受入条件の `then` にある |
| `unbounded` | `UNBOUNDED_TERMS` の語（「常に」「無期限」「always」「never」など）が statement か `then` にある |
| `no_criteria` | 受入条件が無い |
| `no_metric` | `nfr_category` がある要求で、statement にも受入条件にも数値と単位（`400 日`、`10 s`、`4 hours`、`0.1%` など）が無い |
| `compound` | statement の 1 文に義務の語（must / shall / should、しなければならない / すること など）が 2 つ以上ある |

**語彙は日本語と英語だけです。** 英語は単語の境界で、日本語は部分文字列で一致させます。他の言語の文書には規則の検査が働かず、backend の指摘だけになります。

### Claude に送るデータ（spec extract）

| 送信先 | 段 | 送るもの |
|---|---|---|
| Anthropic（Claude CLI 経由） | `extract --backend claude` | `documents.jsonl` の全 section（見出し、本文、front-matter の生の行）、文書の ID・kind・タイトル、明示で取った決定（ID、ADR 番号、タイトル、状態）と要求（ID、statement） |

送る前の禁止語と秘密情報の形の検査、予算、`llm-calls.jsonl` の記録は Review Learning と同じです（上の「Claude に送るデータ」）。文書は 400 KiB ずつの batch に分け、1 文書だけで上限を超えたら切り詰めずに exit 1 です。文書の本文は DATA として送り、本文に「approved にせよ」などの指示があっても状態は変わりません（状態は harness の規則だけで決まります）。

### 未検証（spec）

- 実際の Slack と Confluence の export の全形式。読むのは上に書いた形だけで、Slack の channel ディレクトリ全体や Confluence の HTML export には対応していません。
- 実際の Claude CLI に対する `spec extract`。テストは `guard._run` を差し替えた経路と stub だけを通ります。推定の精度は何も確かめていません。
- 曖昧さの語彙の網羅性。表にある語の検出をテストしているだけで、誤検知率と見落とし率は測っていません。
- 実際の GitHub に対する `spec analyze`。テストは合成リポジトリ（ローカルの path から fetch）と fixture の PR 一覧だけを通ります。`https://<host>/<owner>/<repo>.git` からトークン付きで `refs/pull/<n>/head` を fetch できるか、fork が削除された PR の ref がどう見えるかは実機で確かめていません。
- 指摘の精度。合成の 25 件で期待した件数が出ることを確かめただけで、実際のリポジトリでの誤検知率と見落とし率は測っていません。構造化記述（下の形）を書いていない文書の変更は `impact` にしかなりません。
- `--backend claude` の推定リンクと説明文の質。テストは stub と `guard._run` を差し替えた経路だけを通ります。

## Specification Consistency: トレースと open PR の分析

`spec analyze` は、run の台帳、default branch、全 open PR を読み、`trace.json`（trace-link）と `findings.json` を作ります。`spec report` はそれを `impact-report.json` と `impact-report.md` にまとめます。どちらも合否や状態を LLM に決めさせません。

```bash
# 台帳を作った run に対して（ingest と extract の後）
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec analyze --run run-... --prs open \
  --repo example-org/demo --auth-env SPEC_READ_TOKEN
# => {"status": "complete" | "partial" | "stale", "findings": {...}, "git_calls": N, "diffs": {...}}
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec report --run run-...
# v2 の evaluator adapter として使うとき（標準出力に evaluation.schema.json の $defs/output）
"${CLAUDE_PLUGIN_ROOT}/scripts/sre-harness" spec report --run run-... --format evaluator-output
```

合成データで試すときは `runtime/` で `python -m examples.synthetic.spec_repo <dir>` を実行すると、25 件の open PR を持つ合成リポジトリ（`--git-source` に渡す）と PR 一覧の fixture（`--fixture` に渡す）ができます。`--fixture` と `--git-source <絶対 path>` は組で使い、通信しません。`--auth-env` のときは `https://<host>/<owner>/<repo>.git` から fetch し、トークンは guard が環境変数経由の設定で渡します。`--auth-env` の変数が空なら何も起動せずに exit 2 です。open PR が無いときは `--default-branch` が要ります（PR 一覧の `base.repo.default_branch` から読むため）。

### bare リポジトリの場所と消し方

PR の内容は harness 専用の bare リポジトリから読みます。利用者の checkout には触らず、worktree も作らず、PR の内容をディスクに展開しません（blob は `git cat-file blob <sha>:<path>` でメモリに読みます）。

| 項目 | 場所 |
|---|---|
| bare リポジトリ | `<ルート>/cache/git/<owner>--<repo>--<hash>.git/`（`<hash>` はリポジトリ名と fetch 元から作る 12 桁） |
| fetch する ref | default branch を `refs/sre-harness/heads/<branch>` に、open PR の `refs/pull/<n>/head` を `refs/sre-harness/pull/<n>` に。それ以外は fetch しません。partial clone は使いません |
| diff | run の `diff-cache/<sha256>.diff`。`(merge base, head SHA)` ごとに 1 回だけ計算します |

キャッシュは run をまたいで使います。消すときは `<ルート>/cache/git/` を削除してください（次の実行で作り直します）。git の実行回数は PR 1 件につき固定の数（`rev-parse`、`merge-base`、`diff`）と、読める変更ファイル 1 件につき 2 回（merge base 側と head 側の blob）で、PR の組の総当たりはしません。`analyze.json` の `git_calls` と `diffs` に実測が残ります。1 つの diff が 2 MiB、1 ファイルが 2 MiB、run の diff の合計が 64 MiB を超えたら、切り詰めずに exit 1 で止まります。

### 読むファイルと構造化記述の形

path で種類を決め、それ以外のファイルは読みません。

| 種類 | path | 読むもの |
|---|---|---|
| Markdown | `*.md` | 見出しで section に分けます。見出しが ID（`REQ-0001`、`ADR-0006`、`DEC-0001`）で始まる section とその下位の section はその entity を定義し、front-matter に `id: ADR-0006` がある文書は全体がその決定です。それ以外の section は文書自体（`design`）です。section 中の ID は参照です |
| IaC | `*.tf` | トップレベルの `resource "type" "name" {`、`data "type" "name" {`、`module "name" {` の block を 1 単位にし、resource アドレス（`aws_s3_bucket.logs`）を node にします。直前のコメント行と block 内のコメントの ID は参照です |
| テスト | `tests/`、`test/`、`spec/` の下、`test_*.py`、`*_test.py`、`*_test.go`、`*.test.[jt]s` など | `def test_...`、`func Test...`、`it("...")`、`test("...")` を 1 単位にします。テスト名の中の ID（`test_req_0001_...`、`req-0001`）は「そのテストが要求を検証する」参照です |

PR が要求の値を変えたことは、次の構造化記述だけから読みます。

- Markdown の `- key: value` の箇条書き（`key` は小文字・数字・`_`）。見出しを辿って最も近い「ID を 1 つだけ含む見出し」の entity の値です（例: `## REQ-0001: ログの保持期間` や `## アーカイブの保持（REQ-0001）` の下の `- retention_days: 400`）。ID を含む見出しが無ければ front-matter の `id` の entity、それも無ければ値として読みません。`given` / `when` / `then` / `and` は値にしません。
- front-matter の `id` 以外のキー（`title`、`status`、`date`、`supersedes`、`approvers`、`systems`、`components`、`environments`、`attendees`、`tags` を除く）。
- IaC の block のトップレベルの属性で、行末のコメントに ID があるもの（`retention_days = 400 # REQ-0001`）。block の上の `# REQ-0001` は参照（トレース）だけで、値にはなりません。

同じ entity の同じキーは、Markdown と IaC で同じ名前にしたときだけ同じものとして扱います。読めない変更（文の書き換え、表の値、入れ子の属性）は `impact` に留め、推測で `semantic_conflict` にしません。

### トレース（explicit と inferred）

trace-link は Decision → Requirement → Design → IaC → Test のつながりで、下の層から上の層へ向きます（`refines`、`implements`、`verifies`、`depends_on`、`conflicts_with`）。

- **explicit** は規則で取れたものだけです。台帳の `decision_refs`、ファイル中の ID 参照（front-matter の `supersedes` は `conflicts_with`）、Markdown の section 中に書かれた resource アドレスやリポジトリの path（`infra/logs.tf`）、テスト名の中の ID です。
- **inferred** は `--backend` を付けたときだけ作ります。ID を 1 つも書いていない default branch の Markdown を backend に渡し、提案のうち、引用が文書の部分文字列で、宛先が台帳にあるものだけを `confidence: low` で残します（除いたものは `analyze.json` の `rejected_links`）。stub は「決定か要求のタイトル（7 文字以上）を含み、その ID を含まない文書」をつなぐだけで、LLM の品質を何も示しません。
- 報告では explicit と inferred を別の列に出します。inferred の根拠だけで成り立つ指摘は `basis: inferred` になり、重大度は `medium` を上限にします。`critical` と `high` の唯一の根拠にはなりません（builder、`findings.check`、impact-report の schema の 3 か所で検査しています）。

### 指摘の種類

重大度は種類で固定です（`runtime/sre_harness/spec/findings.py`）。

| kind | 重大度 | 規則 |
|---|---|---|
| `semantic_conflict` | critical | 2 つ以上の open PR が、同じ entity の同じ構造化キーを互いに違う値にする。候補は (entity, key) の索引から決定論的に出します（PR の組の総当たりはしません）。同じ merge base の上で diff の hunk が重なるか隣り合う組は git が merge で衝突として示すので数えず、`limitations` に書きます。同じ値にする PR 同士は競合しません。backend は説明文だけを書きます |
| `contradiction` | high | default branch のファイルの値が台帳の値（台帳の元の section の構造化記述）と違う。台帳に値が無いときはファイル同士で値が違う |
| `superseded_dependency` | high | superseded な決定に依存する要求・文書・IaC・テスト（決定から決定への参照は除く）。PR では、PR が新しく足した参照だけを数えます |
| `missing_test` | high | PR が要求の構造化記述を変えたのに、その要求の ID を名前に持つテストを PR が変えていない（explicit なトレースだけで判定） |
| `stale_sha` | high | PR 一覧の head SHA と fetch した `refs/pull/<n>/head` が違う（一覧と fetch の間に head が動いた）。その PR は分析せず、run は `stale` で exit 1。他の PR の分析は出します |
| `not_reflected` | medium | approved で有効な決定か explicit な要求の ID を default branch のどのファイルも書いていない、または台帳の構造化記述のキーがどのファイルにも無い |
| `ambiguous_requirement` | medium | 台帳の要求に `ambiguity` がある（規則の検出は explicit、LLM の検出は inferred） |
| `input_missing` | medium | 台帳に無い ID への参照、extract の `input_missing`、head を fetch できなかった PR（fork の削除など。run は `partial` で exit 1） |
| `impact` | info | PR が変えた単位からトレースを辿って届く決定と要求 |

### 報告

`impact-report.json`（schema `impact-report`）は `run_id`、`generated_at`、`criteria_hash`（規則の版、重大度の表、inferred の上限、台帳の hash）、`default_branch{name, sha, moved_since_analysis}`、`prs[{number, head_sha, merge_base, analyzed_head_sha, stale}]`、`findings[]`、`limitations[]` を持ちます。`patches` と `audit` は後続の段が埋めます。`moved_since_analysis` は `spec report` が fetch しないので常に false です。

`impact-report.md` に載せる外部入力（PR タイトル、文書の引用、ファイルの値、説明文）は、表のセルではリンク・画像・HTML・`|`・バッククォートの記法をエスケープし、`://` を文字参照にします。引用と説明文は、本文中のバッククォートより長い fence のコードブロックに入れます。findings.json の `status` が `stale` か `partial` なら `spec report` も exit 1 です。

`--format evaluator-output` は toshipon-mode の Autonomous Improvement Harness v2 の `evaluation.schema.json` の `$defs/output` の形で、種類ごとの件数（`findings_<kind>`）、`findings_total`、`findings_inferred`、`prs_total`、`prs_stale`、`prs_unanalyzed` と、指摘ごとの evidence（`impact-report.json#FD-0001`）を出します。分析できた PR が 0 件なら `unevaluable` を付けます。

### Claude に送るデータ（spec analyze）

| 送信先 | 段 | 送るもの |
|---|---|---|
| Anthropic（Claude CLI 経由） | `analyze --backend claude` | ID を書いていない default branch の Markdown の全文と path、台帳の決定と要求の ID とタイトル。意味的競合の候補があるときは、entity、キー、PR 番号、PR タイトル、値 |

`--backend` を付けなければ何も送らず、説明文は harness の定型文です。送る前の検査、予算、`llm-calls.jsonl` の記録は extract と同じです（`stage` は `analyze`）。

## IaC Delivery: 状態遷移と証跡の契約

IaC の変更が「要求承認 → 設計 → 実装 → ステージング検証 → PR → 特権者承認 → デプロイ」の順に進んだかを、記録と証跡から検査します。harness が持つのは検査（`iac validate`）だけです。

- `terraform apply`、デプロイ、CodeBuild などのビルドの起動、クラウド API の呼び出しは行いません。
- 状態を進めるコマンド、`deployed` を記録するコマンドはありません。delivery の記録は利用者の仕組みが作り、harness は読むだけです。`iac` グループに `validate` 以外のサブコマンドが無いことはテスト（`tests/e2e/test_iac_validate.py`）で検査しています。
- 状態遷移の合否は表と規則で決まり、LLM は使いません。

```bash
# 合成データを作る（runtime/ で。正しい例と 6 つの不正な例）
python -m examples.synthetic.iac_scenarios /tmp/iac-demo

sre-harness iac validate --plan /tmp/iac-demo/valid/plan.json \
  --evidence /tmp/iac-demo/valid/evidence --delivery /tmp/iac-demo/valid/delivery.json
# => {"run_id": "run-...", "status": "valid" | "violations" | "not_checked", "violations": {...}, "risk": "high" | ... | "unevaluable"}

# v2 の evaluator adapter として（標準出力に evaluation.schema.json の $defs/output）
sre-harness iac validate --plan plan.json --evidence evidence/ --delivery delivery.json --format evaluator-output
```

`--delivery` を省くと、plan のリスク分類だけを行い、`transition-check.json` は `status: not_checked` になります。

### 記録の形

schema は `runtime/schemas/iac-delivery.schema.json`（`$id` は `urn:sre-harness:iac-delivery`）です。

```
change_id, state: requirement_approved | designed | implemented | staging_verified |
                  pr_opened | privileged_approved | deployed | rejected | rolled_back,
transitions: [{ from, to, at, actor: { name, role }, evidence: [{ kind, sha256, locator }] }]
```

- `from` は最初の遷移だけ `null` です。`at` は `YYYY-MM-DDThh:mm:ssZ` です。
- `evidence[].locator` は `--evidence` のディレクトリからの相対 path、`sha256` はそのファイルの bytes の hash です。`kind` は `requirement`、`design`、`commit`、`plan`、`test_result`、`pull_request`、`execution_record`、`note` のどれかです。
- `requirement` の証跡は `{"id": "REQ-0001", "status": "approved"}`、`execution_record` の証跡は `{"plan_sha256": "<64 桁>", "result": "..."}` の形の JSON です。

### 遷移の表

表は `runtime/sre_harness/iac/contract.py` の `TRANSITIONS` の 1 か所にあります。

| from | to |
|---|---|
| （なし） | `requirement_approved` |
| `requirement_approved` | `designed` |
| `designed` | `implemented` |
| `implemented` | `staging_verified` |
| `staging_verified` | `pr_opened` |
| `pr_opened` | `privileged_approved` |
| `privileged_approved` | `deployed` |
| `requirement_approved` から `privileged_approved` までの各状態 | `rejected` |
| `deployed` | `rolled_back` |

`rejected` と `rolled_back` からは進めません。

### 必須証跡と横断の規則

| 遷移先 | 必須の証跡 | 規則 |
|---|---|---|
| `requirement_approved` | `requirement` | 要求の `status` が `approved` |
| `designed` | `requirement`、`design` | 要求の `status` が `approved` |
| `implemented` | `commit` | |
| `staging_verified` | `plan`、`test_result` | |
| `pr_opened` | `pull_request` | |
| `privileged_approved` | `plan` | actor の role が `privileged`。`implemented` の actor と別人。承認した plan の hash が `staging_verified` の plan の hash と一致 |
| `deployed` | `execution_record` | 実行記録の `plan_sha256` が `privileged_approved` の plan の hash と一致 |
| `rejected` | `note` | |
| `rolled_back` | `execution_record` | |

ほかに、どの遷移にも次を検査します。最初の遷移が `null → requirement_approved` であること、各遷移の `from` が前の遷移の `to` と同じこと、`at` が前の遷移より前でないこと（同時刻は可）、`state` が最後の遷移の `to` と同じこと、`--plan` の hash が delivery の最後の `plan` の証跡と同じこと。

### 違反の code

| code | 意味 |
|---|---|
| `schema` | JSON Schema に合わない（未定義の状態を含む）。このときは他の検査をしません |
| `bad_start`、`broken_chain` | 最初の遷移が違う、前の遷移とつながらない |
| `skipped_stage`、`went_back`、`illegal_transition` | 段の飛び越し、逆行、表にない遷移（`rejected` の後など） |
| `time_went_back`、`state_mismatch` | 時刻の逆行、`state` と最後の遷移の不一致 |
| `missing_evidence` | 必須の証跡が無い |
| `evidence_path_refused`、`evidence_missing`、`evidence_symlink`、`evidence_too_large`、`evidence_hash_mismatch` | 絶対 path・`..`・`.`・空の要素・`\` を含む locator、ファイルが無い（ディレクトリを含む）、途中か末尾に symlink がある、16 MiB を超える、hash が違う |
| `evidence_content_invalid`、`requirement_not_approved` | 要求・実行記録の JSON の形が違う、要求が approved でない |
| `approver_not_privileged`、`approver_is_implementer`、`plan_not_staged` | 特権者承認の規則に反する |
| `plan_hash_mismatch`、`plan_mismatch` | 承認時と違う plan をデプロイした、`--plan` が delivery の plan と違う |

証跡は `--evidence` の中だけを読みます。symlink は辿らず、ファイルは読み取り専用で開きます。

### plan のリスク分類（Rego サンプル）

`runtime/policies/iac_risk.rego` は、Terraform の `show -json` 形式の `resource_changes[].change` から変更ごとに水準（`none` < `low` < `medium` < `high`）と理由を返すサンプルです。網羅的な policy ではありません。

| 理由 | 水準 | 条件 |
|---|---|---|
| `delete` | high | `actions` が `["delete"]` |
| `replace` | high | `actions` に `delete` と `create` の両方 |
| `sensitive_type` | high | IAM、security group、S3 の公開設定の resource type で、`no-op` / `read` 以外 |
| `public_setting` | high | `publicly_accessible` などが true になる、`acl` が `public-` で始まる値に変わる |
| `create`、`update` | medium | それ以外の作成・更新 |
| `unknown_action` | medium | 表にない action |
| `tags_only` | low | `tags` / `tags_all` だけが変わる更新 |

状態遷移の検査には Rego を使いません（Python と JSON Schema です）。

### opa の要否

リスク分類は `opa` があるときだけ行います。Python で同じ分類を代わりに行うことはしません（2 つの実装を同期させないため）。

- `opa` が PATH に無いと、`risk.json` は `status: unevaluable` になり、水準を出しません。状態遷移の検査は行い、exit code は違反の有無で決まります。
- `opa` は `guard.py` だけが起動します。argv は固定で（`opa eval --format json --strict --strict-builtin-errors --capabilities <同梱> --data <同梱 policy> --stdin-input data.sre_harness.iac_risk.result`）、plan は標準入力で渡します。policy、query、flag を利用者が指定する経路はありません。環境変数は `PATH`、`LANG`、`LC_ALL`、`TMPDIR`、`SYSTEMROOT` だけを渡し、cwd は空の一時ディレクトリです。
- `runtime/policies/iac_risk.capabilities.json` は、policy が呼ぶ builtin（`eq`、`equal`、`neq`、`internal.member_2`、`is_object`、`is_string`、`object.remove`、`startswith`、`max`、`sort`、`or`）だけを並べた許可リストで、`allow_net` は空です。`http.send`、`net.lookup_ip_addr`、`opa.runtime` などを policy に書くと、opa が `undefined function` でコンパイルを拒否します（opa v1.22.2 で確認）。policy で新しい builtin を使うときは、`opa capabilities --current` の該当する定義をこのファイルに足します。
- CI（`.github/workflows/sre-harness.yml`）は opa v1.22.2 の Linux amd64 static バイナリを版固定の URL から取得し、公式の `.sha256` の値で検査してから PATH に置きます。`SRE_HARNESS_REQUIRE_OPA=1` を付けて pytest を実行するので、CI では opa のテストが SKIP されず、opa が無ければ失敗します。手元で opa が無いと、opa を使うテストは理由付きで SKIP と表示されます。

### 出力

`transition-check.json`（schema `iac-delivery` の `$defs/transition_check`）は `status`（`valid` / `violations` / `not_checked`）、`change_id`、`state`、`delivery_sha256`、`plan_sha256`、`transitions_checked`、`violations[{id, code, transition, message, locator}]` を持ちます。`risk.json`（`$defs/risk`）は `plan_sha256`、`status`（`evaluated` / `unevaluable`）、`engine`（policy と capabilities の hash、query）、`level`、`changes[{address, type, actions, level, reasons}]` を持ちます。

`--format evaluator-output` の `metrics` は `contract_violations`、`transitions_checked`（`--delivery` があるとき）と、`risk_high`、`risk_medium`、`risk_low`、`risk_none`、`risk_changes_total`（opa があるとき）です。違反は 1 件ずつ evidence（`transition-check.json#IV-0001`）になります。どちらも無いときは `metrics` を空にして `unevaluable` に理由を入れます。

### 未検証（IaC）

- 実際の Terraform が出す plan JSON で動かしていません。合成の plan（`examples/synthetic/iac_scenarios.py`）だけで確認しています。
- CI の opa の取得 step は GitHub Actions 上では未実行です。取得元の URL と checksum は手元で同じバイナリを取得して確かめました。
