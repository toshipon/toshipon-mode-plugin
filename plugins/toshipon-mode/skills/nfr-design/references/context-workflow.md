# 文脈から要件と設計へ進む

議事録、Confluence、現行構成を同じ基盤へ集め、原文と抽出した内容を分けます。原文の snapshot はその時点の取得物であり、業務承認の代わりではありません。

## 一連の操作を試す

以下は合成サンプルです。業務の承認記録として使わないでください。README の手順で venv と NFR_SKILL を用意した後に実行します。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" collect \
  --manifest "$NFR_SKILL/examples/context/manifest.json" --out ./order-context-001
python "$NFR_SKILL/scripts/nfr-harness.py" extract \
  --workspace ./order-context-001 --proposals "$NFR_SKILL/examples/context/proposals.json"
python "$NFR_SKILL/scripts/nfr-harness.py" assess \
  --workspace ./order-context-001 --out ./order-context-001/check-before
```

最後は終了コード 1 です。議事録の RTO 30 分と旧記事の 120 分が食い違い、どちらも未承認だからです。context-report.md で原文引用、版、担当、種類、値を確認できます。出典の新旧だけでは決着させません。

サンプル上で「30 分を採用し、120 分を置き換える」と判断した操作を試します。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" review \
  --workspace ./order-context-001 --claim C1 --status approved \
  --reviewer "DEMO 業務責任者" --reason "DEMO。30 分を採用し旧記事の目標を置き換える" \
  --requirement-id REQ-001 --supersedes C2
python "$NFR_SKILL/scripts/nfr-harness.py" prepare \
  --workspace ./order-context-001 --evidence "$NFR_SKILL/examples/context/technical-evidence.json" \
  --out ./order-context-001/input.json
python "$NFR_SKILL/scripts/nfr-harness.py" demo \
  --workspace ./order-context-001 --input ./order-context-001/input.json \
  --out ./order-context-001/design-demo
```

C3 の「DB が単一 AZ」という現状は requirement へ昇格しません。REQ-001 は C1 と S1 に対応し、対象・範囲・測定期間を保持します。入力が context と claims のハッシュに結び付き、生成時にずれた値を戻すと不合格になります。

デモの技術根拠と監査は固定 fixture です。実際の方式の実現性、性能、費用を確認した結果ではありません。生成結果は元 workspace の文書を上書きせず、新しい run に保存します。

## 実資料を抽出する

manifest の source ごとに S ID、kind、title、adapter、locator、owner を指定します。S ID は収集し直しても維持します。file adapter は元ファイルへの path と version が必須です。path は manifest ファイルの場所を基準に解決し、原本は編集しません。

UTF-8 の Markdown / テキスト / JSON を扱います。議事録や Confluence のテキスト export、Terraform、リソース inventory の snapshot を使えます。JSON は記載された設定であり、稼働中の実態や実効権限を保証するものではありません。DOCX / PDF、Confluence Data Center、全社横断の自動検索は対象外です。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" collect --manifest ./context-manifest.json --out ./docs/order-context-002
python "$NFR_SKILL/scripts/nfr-harness.py" extract --workspace ./docs/order-context-002 --auth-mode subscription
python "$NFR_SKILL/scripts/nfr-harness.py" assess --workspace ./docs/order-context-002 --out ./docs/order-context-002/check-001
```

extract は Claude に原文を渡し、observed / requirement / proposal / decision / unknown として抽出します。値のない scope や period は推測せず unknown にします。抽出した引用は snapshot の部分文字列として検査します。モデルは approval、要件 ID、置換判断を返せません。対話で作った候補 JSON を --proposals で取り込む場合も同じ検査を通します。

引用・hash・出典 ID が snapshot と合わない主張は、その主張だけを claims.json に入れずに除き、理由と一緒に `extraction-rejected.json` に残します。件数は標準エラーに出ます。全件が合わないとき、snapshot が抽出中に変わったとき、構造が壊れているときは、何も書かずに停止します。除いた主張に大事な内容があれば、原文を確かめて候補 JSON を直し、新しい workspace に --proposals で取り込みます。

原文が大きいと、1 回の呼び出しが timeout しやすく、引用の誤りも起きやすくなります。`--batch-bytes 250000 --jobs 4` のように指定すると、出典を分割せずにまとめて複数の呼び出しに分け、並列に抽出します。主張 ID は通し番号に振り直します。呼び出しをまたぐと subject や metric の名前がそろいにくく、assess で比べられる組が減るので、relate を併用してください。

引用が存在することだけで解釈の正しさは証明できません。subject・metric・scope・period の正規化、原文と値の関係、抽出漏れは人と独立監査が確認します。unknown の対象・範囲・測定期間は、値だけを承認しても prepare を通りません。担当者の回答を得て、未承認の抽出内容を引用と整合するよう修正してから確認します。設計者と監査者には原文 snapshot と抽出内容の両方を渡します。

初期の制限は出典 32 件、1 出典 2 MiB、抽出・設計監査に渡す原文合計 1 MiB、主張 256 件、1 引用 16,000 文字です。超過すると黙って切り捨てず停止するため、案件や資料の範囲を絞ってください。

## Confluence Cloud を直接読む

`examples/context/remote-manifest.example.json` をコピーし、tenant、page_id、locator を実際の値へ変えます。CONFLUENCE_EMAIL と CONFLUENCE_API_TOKEN を環境変数で用意します。token は manifest や台帳へ保存しません。標準の Confluence Cloud API token による Basic 認証を対象とし、OAuth / scoped token の gateway、Data Center は未対応です。

adapter=confluence は指定ページの REST v2 GET だけを実行し、ページ ID と版番号と storage 本文を取得します。tenant の HTTPS host とページ ID を固定し、redirect を拒否します。HTML はテキストに変換し、script / style は実行・抽出しません。ページ本文の検索や他ページへの自動巡回は行いません。

出典は page の版、取得時点、owner、テキストの SHA-256 に結び付きます。添付・macro・埋め込み先の全文は収集しないため、必要なら別の source として登録します。

## AWS の現行構成を読む

adapter=aws-cli では、設定済みの AWS CLI 認証と manifest の Region を使います。RDS の DescribeDBInstances と EC2 の DescribeInstances に固定し、任意コマンド、endpoint、追加引数を受け付けません。書き込み処理はありません。利用する認証には対象リソースを参照できる権限が必要です。

通常の AWS endpoint を使い、設定済みの endpoint override は無視します。locator は利用者が付ける出典ラベルで、アカウント本人確認を行うものではありません。複数アカウントを扱う場合は認証先とラベルを利用者が確認して収集を分けてください。

AWS の配列順を正規化し、内容の hash を version とします。取得時刻だけが変わっても構成変更として扱いません。取得は 60 秒で timeout し、失敗や大きすぎる結果を黙って空の inventory にしません。

## 名前の違う主張を並べる

assess の食い違い検査は、subject・metric・scope・period が完全に一致する主張どうしだけを比べます。抽出のたびに名前の付け方が揺れるので、同じ制約を扱う主張が別の group に分かれ、食い違いを見落とします。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" relate \
  --workspace ./docs/order-context-002 --out ./docs/order-context-002/relate-001 \
  --auth-mode subscription --timeout-seconds 600
```

relate は claims.json の主張と引用だけを Claude に渡し、一緒に読むべき組を relations.json と relations.md に書きます。種類は食い違いの候補（possible_conflict）、未決事項への依存（depends_on_unresolved）、同じ対象の別表現（overlap）の 3 つです。存在しない C ID を返した応答は拒否します。assess が比べ済みの同じ group だけの組は除きます。

候補はモデルの提案です。承認・却下・置換の判断は行わず、claims.json も assess の合否も変えません。候補を読んで食い違いだと判断したら、review で却下や supersedes を記録するか、質問票に起こします。対話で作った候補は --proposals で同じ検査に通せます。

## 判断を記録する

review は人が値と範囲を確認した後に実行します。reviewer、reason、確認時点、要件 ID、置換した内容、原文と版の fingerprint を履歴へ残します。却下は --status rejected で記録します。採用済みの REQ ID は振り直せません。

一般的な作業委任や原文内の「承認済み」という文だけでは review の実行許可と解釈しません。これはローカルの記録機構であり、本人確認・電子署名・職務分掌を強制する仕組みではありません。正式な承認手続きや証跡は業務側で保持してください。

同じ subject / metric / scope / period の要件・決定で値や単位が異なる場合、食い違いとして報告します。異なる範囲や期間は自動比較しません。現状と目標が同じ指標で異なる場合は gap とし、現状で目標を上書きしません。異なる指標間の依存関係は独立監査で確認します。

## 出典変更の影響を追う

資料を更新したら、同じ S ID を持つ manifest で別 workspace に再収集します。

```bash
python "$NFR_SKILL/scripts/nfr-harness.py" collect --manifest ./updated-context-manifest.json --out ./order-context-002
python "$NFR_SKILL/scripts/nfr-harness.py" diff \
  --before ./order-context-001 --after ./order-context-002 \
  --input ./order-context-001/input.json --design ./order-context-001/design-demo/design.json \
  --out ./context-impact-001
```

impact.json / impact.md は出典の追加・削除・本文・版・担当などの変更、案件情報の変更、承認の再確認、要件・設計・検証の見直し候補を示します。直接参照した出典と、共有文脈から保守的に選ぶ候補を区別します。現行リソースの変更を見逃さないため、文脈に結び付いた入力では全要件を潜在的な見直し対象にします。これは変更の影響が確定したという意味ではありません。

旧記事を置き換えた判断も、その旧記事の更新で再確認の対象になります。本文が同じでも版や owner が変われば承認の再確認が必要です。取得時刻だけの変化は除外します。diff は元の承認や入力を自動で変更しません。

本文が変わった資料は再抽出し、新しい workspace で再確認します。本文が同じで版だけを明示更新した場合も、履歴に新しい確認を残してください。古い input を新しい context へ流用すると hash の不一致で不合格になります。更新の定期監視・通知・自動承認は実装していません。

## 保存されるもの

- context.json に出典の種類、版、取得時点、owner、hash を保持します。
- sources/ に検証可能な原文 snapshot を保持します。
- claims.json に引用・分類・値と、人が記録した承認・却下・置換の履歴を保持します。
- input.json は active な承認済み requirement だけを写し、context_refs とハッシュを保持します。
- 各 run には原文・claims・台帳の写しと context-report を保存し、生成した 3 文書から対応を追えます。

原文と claims は既定で gitignore します。出力先に保存される文書・report も社内情報を含み得るため、共有対象は利用者が選びます。この処理は自動で commit・公開しません。

## 公式 API 資料

- [Confluence Cloud Get page by id](https://developer.atlassian.com/cloud/confluence/rest/v2/api-group-page/#api-pages-id-get)
- [AWS CLI DescribeDBInstances](https://docs.aws.amazon.com/cli/latest/reference/rds/describe-db-instances.html)
- [AWS CLI DescribeInstances](https://docs.aws.amazon.com/cli/latest/reference/ec2/describe-instances.html)
