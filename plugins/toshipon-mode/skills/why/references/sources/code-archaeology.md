# Code Archaeology (git + in-repo)

## What this source contains

- コミット履歴(メッセージ、日付、著者、diff)
- PR の説明、レビューコメント、議論のスレッド(`gh` 経由)
- インラインコードコメント、TODO、FIXME、非推奨の注記
- リポジトリが保持していれば ADR(アーキテクチャ決定記録)
- テスト。名前とアサーションはしばしば変更を動機づけたエッジケースをエンコードしている
- 同じコミットで変更された関連ファイル(co-change シグナル)
- リポジトリ内の CHANGELOG エントリ、リリースノート
- コミットメッセージや PR 本文で言及される issue/チケット ID

コードに直接結びついているため最も信頼でき、最も網羅的な情報源。リポジトリを通ったものはすべてここにあるはずである。

## How to search it

シードのコミットリストを拡張する:

```bash
# リネームを含むファイルの全履歴
git log --follow --oneline -- <file>

# pickaxe: この正確なテキストを追加または削除したコミット
git log -S '<exact_string_from_code>' -- <file>

# あるいはパターン用に:
git log -G '<regex>' -- <file>

# 誰がいつ各行を書いたか
git blame -L <start>,<end> <file>

# 特定コミットの全 diff
git show <hash>

# このファイルに影響した 2 点間のコミット
git log <old>..<new> -p -- <file>
```

実質的な各コミットについて、PR の文脈を取得する:

```bash
# マージコミットまたはブランチから PR 番号を見つける
git log -1 --format=%B <hash>

# PR の全文脈: 本文、レビューコメント、紐づく issue
gh pr view <number> --json title,body,author,createdAt,mergedAt,labels,closingIssuesReferences,comments,reviews,files

# --json の reviews と comments フィールドに本当のシグナルがある
```

帯域外のドキュメントを探す:

```bash
# ADR はしばしば docs/adr/ などにある
rg -l -i 'architecture.decision' --glob '*.md'

# 対象周辺の TODO と FIXME
rg -n -C2 '(TODO|FIXME|HACK|XXX|NOTE)' <target_file>

# 関連するテスト。名前がしばしば「なぜ」をエンコードしている
rg -l '<symbol>' --glob '*test*'
```

## What good evidence looks like here

- 変更内容だけでなく解決しようとしている問題を説明する PR の説明(「これはユーザーが 1000 件超のアイテムを持つ場合にページネーションできなかったバグを修正する」)
- 代替案が議論された長いレビュースレッド
- 対象行の近くにある、自明でない制約を説明するインラインコメント
- `test_handles_edge_case_when_X` のような、コードを動機づけたエッジケースを明らかにするテスト名
- チケットやインシデント ID を参照するコミットメッセージ
- ユーザー向けの根拠をまとめた CHANGELOG エントリ

## Common pitfalls

- **スカッシュマージによる平坦化。** リポジトリが PR をスカッシュしている場合、ブランチ履歴の個々のコミットは失われる。PR の本文とコメントにフォールバックする。
- **誤解を招くコミットメッセージ。** 「Small refactor」が意図的な振る舞いの変更を隠していることがある。メッセージではなく diff を見ること。
- **カーゴカルト的なパターン。** 著者が理由を理解せずにパターンをコピーしただけかもしれない。そのパターンがコードベースの以前の箇所に由来するかを確認し、*その*コミットを調査する。
- **ボットのコミットと自動マージ。** Dependabot、Renovate、自動バックポートは通常動機を持たない。意図を探すときはスキップする。
- **コードを意図の証拠として扱うこと。** コード自体はそれが存在する理由の証拠ではない。証拠はコミットメッセージ、PR、コメント、テスト、ドキュメントから来る。「関数が X という名前だから」を意図の証拠として引用しないこと。

## What to return

質問に関わるすべてのコミット/PR/コメントについて、以下を添えて:
- 正確なテキスト(引用)
- ハッシュ / PR 番号 / file:line
- 著者と日付
- 直接的(質問に明示的に答える)か、状況証拠的か
