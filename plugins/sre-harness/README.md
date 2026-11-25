# sre-harness

GitHub のレビュー履歴と仕様文書を読み、改善候補と影響範囲の報告を作る、読み取り専用の harness です。次の 3 つを同じ runtime で動かします。

- Review Learning: 指定期間の PR、レビュー、返信、解決状態、修正前後の差分から、指摘を構造化し、Rule や Skill の候補を作って評価します。
- Specification Consistency: 決定と要求の台帳を作り、default branch と open PR にまたがる未反映、矛盾、古い決定への依存を報告します。
- IaC Delivery: 状態遷移と証跡の契約だけを定義します。apply やデプロイは行いません。

この版（0.1.0）が持つのは、足場と安全境界です。収集以降の段は後続の変更で足します。

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
| GraphQL | `runtime/sre_harness/github/queries/*.graphql` を POST します。query を入力から組み立てません | 同上 |
| git | `guard.GIT_SUBCOMMANDS` にあるサブコマンドと flag だけです。`apply` は `--cached --check` が必須です | 同上 |

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

launcher は Python を `-I`（isolated mode）で起動し、`runtime/` だけを `sys.path` に足します。作業ディレクトリ、`PYTHONPATH` などの `PYTHON*` 環境変数、user site-packages は読みません。利用者のリポジトリを作業ディレクトリにして実行しても、そこにある `argparse.py` や `json.py` は harness の中で import されません。`python3 -m sre_harness` で直接起動すると作業ディレクトリが `sys.path` の先頭に入るので、launcher を使ってください。

## 認証

GitHub を読む段は `--auth-env <環境変数名>` が必須で、既定値はありません。その変数が空なら、通信せずに exit 2 で止まります。

```bash
export REVIEW_READ_TOKEN=...   # 読み取り専用の fine-grained PAT
```

渡した値は、`gh` には `GH_TOKEN` として、`git` には環境変数経由の設定として渡ります。argv、remote の URL、リポジトリの config、ログ、run ディレクトリには書きません。`gh` には空の設定ディレクトリを、`git` には空の global / system 設定を見せます。子プロセスの stderr は保存しません。

git の https の fetch 元は、許可した host（既定は `github.com`。呼び出し側が host を明示した場合はその host）の上にあるものだけを受け付け、トークンはその host にだけ付けます。別の host の https remote は exit 3 で拒否します。ローカルのディレクトリを fetch 元にするとき（利用者が明示する読み取り専用の入力）は、トークンを付けません。ref と refspec は `..`、`@{`、空の要素（`//`）、`.` で始まる要素、末尾の `.lock` を拒否します。

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
