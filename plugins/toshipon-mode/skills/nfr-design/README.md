# 非機能要件と AWS 方式設計の Harness

既存の Doc workspace を入力に、非機能要件定義書・AWS 方式設計書・検証計画を生成します。Claude Code の設計者と監査者を別プロセスで呼び、JSON Schema と Python 評価器で検査します。既定 3 回の反復で合格しなければ、指摘を残して終了します。

文書品質の通過、業務承認、実際の性能や復旧目標の達成は別です。AWS の変更、外部へのメッセージ送信、PR のマージ、デプロイは行いません。

## 議事録・Confluence・現行構成から始める

`collect → extract → assess → 人の review → prepare → run` で、原文から承認済み要件を経て設計へ進めます。出典の版・取得時点・owner・hash と、引用に対応する現状・要件・提案・決定を保持します。議事録と記事の値が食い違う場合は、新しい方を勝手に採用せず判断を求めます。

assess が比べるのは subject・metric・scope・period がすべて一致する主張だけです。名前が違っても同じ制約を扱う主張（例えば「この値は Parameter Store に置く」と「検証に使う値は Parameter Store に置かない」）は、`relate` で関連候補として並べます。候補はモデルの提案であり、assess の合否を変えません。

原文の変更は `diff` で要件・設計判断・検証計画の見直し候補に結び付けます。現行構成と目標を区別し、context を使う workspace では承認と hash の検査を省略できません。原文 snapshot は独立監査にも渡し、抽出漏れと解釈のズレを確認します。

**手順・サンプル・接続方法は [文脈の操作手順](references/context-workflow.md) を参照してください。** file adapter、Confluence Cloud の参照専用 API、AWS RDS / EC2 の参照専用 CLI に対応します。実 Confluence / AWS の認証を伴う接続は、この PR の検証では行っていません。

## 対話で使う

Claude Code に plugin を入れ、`/toshipon-mode:nfr-design <workspace>` を呼びます。既存の Doc discovery / Doc question loop が台帳を用意し、architect が設計案を検討し、nfr-auditor が監査します。自動実行では Python が同等の設計・監査の役割を明示的に指定し、native subagent を再帰起動しません。

Python 3.10 以上が必要です。まず plugin 配布での利用を対象にします。APM で runtime・Schema・Agent が元の配置を保たない構成は未検証です。

## 認証不要のデモ

リポジトリの checkout で、以下を実行します。plugin install 済みの場合は `NFR_SKILL` をインストールされた skill のパスに置き換えます。venv とデモ workspace は利用側に作り、plugin cache には書きません。

```bash
NFR_SKILL="$PWD/plugins/toshipon-mode/skills/nfr-design"
python3 -m venv .venv-nfr
source .venv-nfr/bin/activate
python -m pip install -r "$NFR_SKILL/requirements.txt"
cp -R "$NFR_SKILL/examples/workspace" ./nfr-demo
python "$NFR_SKILL/scripts/nfr-harness.py" demo \
  --workspace ./nfr-demo --input ./nfr-demo/input.json --out ./nfr-demo/demo-001
```

1 回目の設計判断を意図的に欠落させ、2 回目で修正します。固定の監査結果と合成の費用・確認状態を使うテスト用デモです。本番設計の見本や実施済みの監査として扱わないでください。出力先は workspace 直下の新しい空ディレクトリに限ります。

## Claude Code による生成

既存の subscription login を使う場合は、次のように指定します。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" run \
  --workspace ./docs/order-api --input ./docs/order-api/input.json \
  --out ./docs/order-api/run-001 --auth-mode subscription --max-iterations 3
```

API key を使う場合は環境変数 `ANTHROPIC_API_KEY` を設定し、`--auth-mode api-key` を指定します。api-key が既定値です。key を入力 JSON や台帳へ保存しないでください。subscription の認証は `claude auth login` で利用者が行います。

subscription は `--safe-mode`、api-key は `--bare` を使います。両方で `--tools ''`、`--disable-slash-commands`、`--strict-mcp-config` と空の MCP 設定、`--no-session-persistence` を指定します。既存の hooks、Skills、外部ツールに依存せず、入力の中の命令を実行しません。権限確認の bypass は使用しません。

各 CLI 起動の timeout は既定で 180 秒です。`--timeout-seconds`（30〜3600）で `run`、`extract`、`relate` ごとに変えられます。原文の合計が 150 KB を超えると extract は 180 秒を超えることがあります。原文がおよそ 300 KB を超える場合は、extract に `--batch-bytes`（16 KiB〜1 MiB）を付けて出典ごとに呼び出しを分け、`--jobs`（1〜8）で並列に実行します。1 反復あたり最大 2 回の CLI 起動で、既定では最大 6 回です。明示指定できる反復数は 1〜10 回です。費用・利用枠の消費はモデルと入出力量に依存し、timeout でも消費される可能性があります。

Claude Code 2.1.317 で CLI フラグと実接続を検証しています。古い CLI でフラグがなければ更新してください。ローカル Schema は Draft 2020-12 です。Claude CLI の Schema 転送では互換性のためトップレベルの `$schema` 宣言だけを除き、ローカルの厳密な検証は維持します。

## 入力と台帳

詳細は [workspace-contract.md](references/workspace-contract.md) と `schemas/`、`examples/workspace/` を参照してください。

- `sources.tsv` に業務上の出典と AWS の技術根拠 URL を記録します。
- `facts.md`、`questions.tsv`、`decisions.tsv` は既存 Doc workspace のものを使います。
- `input.json` は台帳から写した数値要件です。要件 ID を振り直さず、`source_refs` で S ID を指定します。
- `technical_evidence` は入力者が本文と claim を照合した根拠レジストリです。確認者と確認日を残します。生成された候補が根拠を verified に書き換えることは認めません。

不足や矛盾は台帳へ戻します。質問は既存 `question.sh` を使って更新してください。出力先の台帳は run 時点の写しです。正本の代わりに編集しないでください。

## 既存候補の評価

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" evaluate \
  --workspace ./nfr-demo --input ./nfr-demo/input.json \
  --design ./nfr-demo/demo-001/design.json \
  --review ./nfr-demo/demo-001/review.json --out ./nfr-demo/evaluation-001
```

入力、候補、監査を検査し、合格した場合だけ新しい出力先へ文書を生成します。入力・台帳の hash と評価結果も保存します。自動ループと同じく既存 `doc-check.sh` も必須です。終了コードは 0 が合格、1 が品質 gate 不合格、2 が入力・設定エラーです。

## 評価の範囲

Python が検査するものは、Schema、ID 重複と参照、入力要件と根拠の改変、全要件の設計・検証への対応、目標と単位の一致、RTO/RPO と費用上限、根拠の確認状態と AWS 公式 host、出典台帳への対応、未解決の重大質問・指摘です。

可用性 99.9% 以上には 2 AZ 以上を要求する MVP ポリシーがあります。これは AWS SLA やシステム全体の可用性を計算した結果ではありません。異なる復旧スコープや期間を持つ複数の要件は、現時点では同じ aws.rto_minutes / rpo_minutes の値で検査するため、別の入力に分けてください。

意味的な整合性は [監査 rubric](references/aws-review-rubric.md) で確認します。負荷、測定窓、上限下限、対象範囲、根拠の関連性、IAM、運用責任、見積内訳などが対象です。Python は説明文を実システムと照合しません。medium/low 指摘や一般的な assumptions は合格後も残り得ます。採用判断に影響する仮定は high issue または質問票に残す運用です。

確認済みという入力は入力者の申告です。URL 本文の取得、承認者の認証、根拠内容の真偽判定は実装していません。公式 URL の存在だけで verified にしないでください。負荷試験、復旧訓練、AWS Pricing との照合、Terraform / OPA の検査は別工程です。

## 出力と保護する境界

```text
<workspace>/run-001/
  input.json                  入力 snapshot
  sources.tsv facts.md        台帳の写し
  questions.tsv decisions.tsv 台帳の写し
  metadata.json               SHA-256、実行方式、反復数、終了理由
  iteration-01/               候補・監査・評価
  iteration-02/
  evaluation.json             最終評価
  doc-check.txt               既存の文書検査結果
  design.json review.json     合格候補と監査
  requirements.md             非機能要件定義書
  design.md                   AWS 方式設計書
  verification.md             検証計画
```

文書は一時領域で生成し、既存 `doc-check.sh` が成功した後だけ公開します。元の台帳や input が生成中に変わると停止します。壊れた JSON、CLI エラー、timeout、回数上限でも合格にしません。入力、Schema、評価器、rubric は生成者の編集対象ではありません。

既存の `loop-engineering` はマージ・デプロイ・計測のための仕組みです。この Harness は呼び出しません。

## 開発時の検証

```bash
python -m pip install -r "$NFR_SKILL/requirements-dev.txt"
python -m compileall -q "$NFR_SKILL/runtime" "$NFR_SKILL/scripts"
ruff check "$NFR_SKILL"
ruff format --check "$NFR_SKILL"
(cd "$NFR_SKILL" && pyright --pythonpath "$(command -v python)")
python -m coverage run --source="$NFR_SKILL/runtime/harness" \
  -m unittest discover -s "$NFR_SKILL/tests" -v
python -m coverage report
claude plugin validate ./plugins/toshipon-mode
/bin/bash ./plugins/toshipon-mode/skills/loop-engineering/scripts/loop-selftest.sh
```

テストには、認証不要の失敗→修正→合格、既存 doc-check との互換性、任意 cwd・空白パス、入力の途中変更、回答済み質問の出典、根拠の改変、CLI 不正応答と timeout を含みます。実 Claude の応答内容は固定の snapshot テストにしません。
