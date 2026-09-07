# Source playbooks

why スキルは、利用可能な証拠カテゴリごとに 1 investigator を起動し、それぞれが下記のソース固有プレイブックを 1 つ読む。プレイブックは主要な MCP に対する具体例であり、同じカテゴリの別の MCP に合わせて適応させること。

| Category | Playbook | 例として扱う MCP |
|---|---|---|
| Source control history | [`code-archaeology.md`](./sources/code-archaeology.md) | git、`gh` |
| Issue / ticket tracker | [`linear.md`](./sources/linear.md) | Linear(Jira、GitHub Issues、Plane、Shortcut 向けに適応) |
| Long-form documents | [`notion.md`](./sources/notion.md) | Notion(Confluence、Google Docs、Coda 向けに適応) |
| Real-time team chat | [`slack.md`](./sources/slack.md) | Slack(Discord、Microsoft Teams、Mattermost 向けに適応) |
| Infrastructure observability | [`datadog.md`](./sources/datadog.md) | Datadog(New Relic、Honeycomb、Grafana、Splunk 向けに適応) |
| Error / exception tracking | [`sentry.md`](./sources/sentry.md) | Sentry(Rollbar、Bugsnag、Airbrake 向けに適応) |
| Product analytics warehouse | [`databricks.md`](./sources/databricks.md) | Databricks SQL(Snowflake、BigQuery、ClickHouse、dbt 向けに適応) |

横断的:

- [`incident-postmortem.md`](./sources/incident-postmortem.md)。対象コードが防御的に見える場合(null チェック、リトライ、タイムアウト、レートリミット、フィーチャーフラグ、egress ガード、OOM ハンドラ)に追加する。
