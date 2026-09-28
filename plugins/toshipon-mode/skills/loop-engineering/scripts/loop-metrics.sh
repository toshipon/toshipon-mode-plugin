#!/bin/bash
# loop-metrics.sh metric <key> <from YYYY-MM-DD> <to YYYY-MM-DD>
# loop-metrics.sh events <from YYYY-MM-DD>
# loop-metrics.sh list
#
# The loop's only measurement path. The SQL lives here and the agent passes identifiers, so the
# agent cannot change how its own hypothesis is scored. query_id is the sha7 of this file: edit the
# scoring and every record in flight stops matching, which halts judgment instead of silently
# rescoring it. This file ships with the plugin, so a pull request in the target repo cannot
# change it.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 1

query_id="m:$(shasum -a 256 "$0" | cut -c1-7)"
metrics_file="$REPO/$(loop_cfg "$LOOP_YAML" metrics_file "$RECORDS_DIR/metrics.yaml")"
db=$(loop_cfg "$LOOP_YAML" metrics_db)
db_cwd=$(loop_cfg "$LOOP_YAML" metrics_db_cwd .)
table=$(loop_cfg "$LOOP_YAML" metrics_table product_metrics)
[ -z "$db" ] && { echo '{"error":"metrics_db is not set in loop.yaml"}'; exit 1; }

d1() {  # d1 <sql> -> the rows array, empty after three failed tries
  local out i
  for i in 1 2 3; do
    out=$( (cd "$REPO/$db_cwd" && npx wrangler d1 execute "$db" --remote --json --command "$1" 2>/dev/null) ) \
      && echo "$out" | jq -e 'type == "array"' >/dev/null 2>&1 \
      && { echo "$out" | jq '.[0].results'; return 0; }
    sleep $((i * 5))
  done
  return 1
}

case "${1:-}" in
list)
  sed -n 's/^  "\{0,1\}\([^":]*:\{0,1\}[^":]*\)"\{0,1\}:[[:space:]]*$/\1/p' "$metrics_file"
  exit 0
  ;;
events)
  from="${2:?from date required}"
  [[ "$from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo '{"error":"bad date"}'; exit 1; }
  rows=$(d1 "SELECT COALESCE(SUM(sample),0) AS n FROM $table WHERE day >= '$from'") \
    || { echo '{"error":"d1 unreadable"}'; exit 1; }
  jq -n --argjson r "$rows" --arg q "$query_id" --arg f "$from" \
    '{n: ($r[0].n // 0), from: $f, query_id: $q, captured_at: (now | todateiso8601)}'
  exit 0
  ;;
metric) ;;
*)
  echo "usage: loop-metrics.sh metric <key> <from> <to> | events <from> | list" >&2
  exit 2
  ;;
esac

key="${2:?metric key required}"
from="${3:?from date required}"
to="${4:?to date required}"
[[ "$key" =~ ^[a-z_]+:?[A-Za-z0-9/._-]*$ ]] || { echo '{"error":"illegal metric key"}'; exit 1; }
[[ "$from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$to" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] \
  || { echo '{"error":"bad date"}'; exit 1; }
grep -qF "\"$key\":" "$metrics_file" || grep -qE "^  $key:" "$metrics_file" \
  || { echo "{\"error\":\"$key is not declared in $metrics_file\"}"; exit 1; }

# The aggregation is fixed by the key prefix. A count sums the value; a rate divides by the sample;
# anything else is a sample-weighted mean, because each stored row already holds a per-day aggregate.
# count: and rate: are the domain-facing names (closed trades, stale positions, exposure share);
# hits: and errors: are the same maths under the names the HTTP rollup used first.
case "$key" in
  hits:*|count:*)               agg="COALESCE(SUM(value),0)" ;;
  errors:*|rate:*|share:*)      agg="COALESCE(SUM(value)/NULLIF(SUM(sample),0),0)" ;;
  *)                            agg="COALESCE(SUM(value*sample)/NULLIF(SUM(sample),0),0)" ;;
esac

# n_day_max exists because n is only an honest sample count for a flow. A snapshot metric re-counts
# the same population every day, so summing it inflates the sample by the number of days and the
# power gate believes it has evidence it does not have. A snapshot hypothesis uses n_day_max.
rows=$(d1 "SELECT $agg AS value, COALESCE(SUM(sample),0) AS n, COALESCE(MAX(sample),0) AS n_day_max, COUNT(*) AS days FROM $table WHERE metric = '$key' AND day >= '$from' AND day <= '$to'") \
  || { echo '{"error":"d1 unreadable"}'; exit 1; }

jq -n --argjson r "$rows" --arg q "$query_id" --arg k "$key" --arg f "$from" --arg t "$to" \
  '{metric: $k, value: ($r[0].value // null), n: ($r[0].n // 0), n_day_max: ($r[0].n_day_max // 0),
    days: ($r[0].days // 0),
    window: {start: $f, end: $t}, query_id: $q, captured_at: (now | todateiso8601)}'
