# Databricks Analytics & System Tables

## What this source contains

Databricks はプロダクト分析、データパイプライン、ウェアハウステレメトリの層である。Datadog を補完する: Datadog は*インフラ/ランタイム*の視点、Databricks は*プロダクト/データ*の視点(ユーザーが何をしたか、どの実験が走ったか、機能利用がどう推移したか、閾値定数がどこから来たか)。

- **プロダクト分析イベント。** `your_warehouse.events.analytics_track_event`(生データ)、および `<your_analytics_db>.<schema>.<table>` 内のイベントごとに型付けされ重複排除された dbt モデル。ユーザー行動: 機能呼び出し、クリック、accept/reject、送信、クライアント側から報告されたエラー。
- **利用状況と課金イベント。** `your_warehouse.events.usage_event` / `<your_analytics_db>.<schema>.stg_usage_events`; `your_warehouse.events.raw_model_event` / `<your_analytics_db>.<schema>.stg_raw_model_events`。コストや利用量が動機の意思決定向け。
- **実験 / フィーチャーフラグのデータ。** 露出とアウトカムのテーブル。**スキーマは会社ごとに異なる。** 名前を仮定する前に `SHOW TABLES` で確認する。
- **システムテーブル。** `system.query.history`、`system.compute.warehouses`、`system.billing.*`、`system.access.audit`。「このクエリはコストがかかったか?」「誰がどれくらいの頻度で実行したか?」「ウェアハウス負荷はいつスパイクしたか?」に答える。
- **dbt lineage。** `<your_analytics_db>.<schema>` 内のモデルは、どのパイプラインがあるテーブル/フィールドに依存しているかを明らかにする。上流の変更はしばしば下流コードの変更を動機づける。
- **Databricks ノートブック。** コード変更の前にエンジニアが書いた探索的な分析。**SQL MCP からはクエリ不可。** 根拠がノートブックにあると疑われる場合は、ギャップとして名指しする。

## How to search it

Databricks SQL MCP を使う。主なツール: `execute_sql_read_only`。`statement_id` が返ってきたら、再実行するのではなく `poll_sql_result` でポーリングする。

**クエリ前に方向感を掴む。** スキーマは会社ごとに異なるので、テーブル名を信用する前に確認する:

```sql
SHOW TABLES IN <your_analytics_db>.<schema> LIKE '*<keyword>*';
DESCRIBE TABLE <your_analytics_db>.<schema>.stg_<event>;
```

**すべてのクエリを時間で区切る。** これらのテーブルは巨大で、制約のないスキャンはタイムアウトする。`_timestamp`(イベント)または `start_time`(`system.query.history`)を、出荷日を挟むウィンドウ(通常前後 30 日程度、それより広げるのは強い理由がある場合のみ)でフィルタする。

**生テーブルより型付けされた dbt モデルを優先する。** `<your_analytics_db>.<schema>.<table>` は重複排除・型付け・liquid-clustered されている。`your_warehouse.events.analytics_track_event` は重複を含み、型付けされていない `properties_json` を持つ。モデル名のパターン: `stg_<source>_<event_name_with_underscores>`(`<source>` は `app`、`backend`、`website`、`cli` のいずれか)。パターンだけで解決しない場合は `SHOW TABLES` で正確なモデル名を確認する。生テーブルに落とすのは、対応する dbt モデルがまだない場合、または dbt のリフレッシュ遅延内のイベントが必要な場合のみ。

**型付けされた dbt モデルの列の慣習**(これを知っていれば `DESCRIBE` の往復を省ける):

- `_timestamp`、`_id`、`_auth_id`、`_request_id`、`event_name`。すべてのモデルに標準で存在
- `properties_<name>`。型付けされ、アンダースコア区切りのイベントプロパティ(`properties_entrypoint`、`properties_size_bytes` など)
- `context_team_id`、`context_client_version`、`context_country`、`context_client_os`。事前抽出されたクライアントの文脈

### Investigation patterns that tend to pay off

対象に合ったテーブルと列の組み合わせを選ぶ:

1. **イベント利用の推移。** PR マージ前後 ±30 日ウィンドウにわたる関連 `stg_*` モデルの日次件数。マージから 1〜2 日以内にゼロから安定した水準へのステップ関数的な立ち上がりは、その PR が機能を出荷したことの強い状況証拠になる。ゼロへの減衰は非推奨あるいは削除を示唆する。
2. **ガードレール/防御チェックの由来。** PR の**前**14 日間における関連 `properties_<name>` 列の分布(中央値/p99/最大値)。p99 が対象の閾値定数と一致する場合、その数値がデータから選ばれたことを示唆する。
3. **実験/フィーチャーフラグの照会。** `SHOW TABLES ... LIKE '*experiment*'` で露出テーブルを見つけ、PR の日付近くの該当フラグキーのバリアントごとの露出数を取得する。
4. **マイグレーション、バックフィル、パフォーマンス書き換えのクエリ履歴の証拠。** `system.query.history` を `statement_text ILIKE '%<table_or_symbol>%'` でフィルタし、タイトな `start_time` ウィンドウで、その変更を動機づけたと思われる高コストなクエリを明るみに出す(`total_duration_ms` でソート、あるいは `SUM(read_bytes)`、`COUNT(*)` を集計)。
5. **dbt lineage。** 対象が `<your_analytics_db>.<schema>` のモデルから読み書きしている場合、そのモデル自身の(このリポジトリ内の)git 履歴がしばしば根拠を持つ。自分で追いかけずに、そのリードを git の investigator に渡す。

## What good evidence looks like here

上記のパターンの形に加えて:

- エラーを分類するイベントの件数が、防御コードの PR の後の数日でほぼゼロに落ちる。その PR がそのエラークラスを解決したことを示唆する
- 露出テーブルの行が、PR の出荷日付近の「shipped」/「concluded」判断とともに、対象のフィーチャーフラグキーを名指ししている

## Common pitfalls

- **計測されている ≠ 原因である。** イベントが存在するのは誰かがそれを記録する価値があると考えたからであり、対象コードが*それが原因で*存在するとは限らない。因果を主張する前に、git investigator からの PR/コミットの引用と組み合わせること。
- **サイレントな計測変更。** イベント件数のステップ関数は、ユーザー行動が変わったのではなく、新しいイベントの記録が始まっただけかもしれない。同じウィンドウ内に計測用の PR がないか確認してから、立ち上がりを機能出荷のシグナルとして読むこと。
- **スキーマドリフト。** イベントプロパティは進化する。今日の型付けされた dbt モデルにある列が、対象が書かれた当時には存在しなかったかもしれない。古いデータではそのプロパティが生の `properties_json` の中にしかないことがある。
- **会社ごとのテーブル。** 実験、フィーチャーフラグ、課金、利用状況のテーブルは会社ごとに異なる。存在を確認していないテーブルからの結果を報告するのはよくある失敗パターンである。まず `SHOW TABLES` / `DESCRIBE TABLE` で確認すること。
- **保持期間の崖。** 該当ウィンドウがテーブルの保持期間や dbt モデルの作成日より前なら、それは*ギャップ*であって null 結果ではない。統合者が「結果なし」を「活動なし」と読まないよう、明示的に名指しすること。
- **ノートブックはクエリ不可。** SQL MCP は Databricks ノートブックを見ることができない。根拠がノートブックにあると疑われる場合は、ギャップとして返す。

## What to return

関連する発見ごとに:
- 種類(プロダクトイベント / 実験露出 / 利用状況・課金イベント / システムテーブルの行 / dbt モデル)
- 完全修飾テーブル名と実行した正確なクエリ
- 照会した時間ウィンドウ
- コンパクトな数値サマリー(件数、パーセンタイル、first/last-seen タイムスタンプ)。**生の行をダンプしないこと。**
- 対象の出荷日との時間的相関(例: 「最初の行 2024-08-15; PR #49074 merged 2024-08-14」)
- 関連性と強さ: direct / circumstantial / weak
