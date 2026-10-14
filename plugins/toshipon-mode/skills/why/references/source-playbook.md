# Source playbooks

why スキルは、利用可能な証拠カテゴリごとに 1 investigator を起動し、それぞれが下記のソース固有プレイブックを 1 つ読む。プレイブックは主要な MCP に対する具体例であり、同じカテゴリの別の MCP に合わせて適応させること。1 つのカテゴリに複数のプレイブックがある場合は、環境で見つかったツールに合う方を選ぶ。

| Category | Playbook | 例として扱う MCP |
|---|---|---|
| Source control history | [`code-archaeology.md`](./sources/code-archaeology.md) | git、`gh` |
| Issue / ticket tracker | [`linear.md`](./sources/linear.md) | Linear(GitHub Issues、Plane、Shortcut 向けに適応) |
| Issue / ticket tracker | [`jira.md`](./sources/jira.md) | Jira(Atlassian MCP) |
| Long-form documents | [`notion.md`](./sources/notion.md) | Notion(Google Docs、Coda 向けに適応) |
| Long-form documents | [`confluence.md`](./sources/confluence.md) | Confluence(Atlassian MCP) |
| Real-time team chat | [`slack.md`](./sources/slack.md) | Slack(Discord、Microsoft Teams、Mattermost 向けに適応) |
| Infrastructure observability | [`datadog.md`](./sources/datadog.md) | Datadog(New Relic、Honeycomb、Grafana、Splunk 向けに適応) |
| Error / exception tracking | [`sentry.md`](./sources/sentry.md) | Sentry(Rollbar、Bugsnag、Airbrake 向けに適応)。専用のトラッカーが無く観測基盤がエラーを集約している場合は、[`datadog.md`](./sources/datadog.md) の Error Tracking を infra observability の investigator が兼ねる |
| Product analytics warehouse | [`databricks.md`](./sources/databricks.md) | Databricks SQL(Snowflake、ClickHouse、dbt 向けに適応) |
| Product analytics warehouse | [`bigquery.md`](./sources/bigquery.md) | BigQuery(MCP か `bq` CLI) |

横断的:

- [`incident-postmortem.md`](./sources/incident-postmortem.md)。対象コードが防御的に見える場合(null チェック、リトライ、タイムアウト、レートリミット、フィーチャーフラグ、egress ガード、OOM ハンドラ)に追加する。
