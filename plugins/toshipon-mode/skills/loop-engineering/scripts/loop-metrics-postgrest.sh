#!/bin/bash
# loop-metrics-postgrest.sh metric <key> <from YYYY-MM-DD> <to YYYY-MM-DD>
# loop-metrics-postgrest.sh events <from YYYY-MM-DD>
# loop-metrics-postgrest.sh list
#
# The same contract as loop-metrics.sh, for a repo whose rollup lives in Postgres behind PostgREST
# (Supabase) rather than in D1. loop-run.sh installs this file as the tick's loop-metrics.sh when
# loop.yaml says metrics_backend: postgrest.
#
# It is a second file, not a branch inside loop-metrics.sh, because query_id is the sha of the
# reader. Editing the D1 reader to add a backend would change its sha and stop judgment on every
# record already in flight on D1.
#
# The rows are (day, metric, value, sample), one per metric per day, served by a table or view the
# repo defines outside allowed_paths. The agent passes identifiers; the aggregation is here.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 1

query_id="m:$(shasum -a 256 "$0" | cut -c1-7)"
metrics_file="$REPO/$(loop_cfg "$LOOP_YAML" metrics_file "$RECORDS_DIR/metrics.yaml")"

# Where the key is sent, and which key, come from origin/main and never from the worktree. The tick
# can edit the worktree's loop.yaml; a reader that followed it would hand the key to any host the
# edit names, and could be pointed at any other op:// secret the service account can read.
policy=$(mktemp)
trap 'rm -f "$policy"' EXIT
git -C "$REPO" show "origin/main:${LOOP_YAML#"$REPO"/}" >"$policy" 2>/dev/null \
  || { echo '{"error":"loop.yaml is not readable from origin/main"}'; exit 1; }
base=$(loop_cfg "$policy" metrics_rest_url)
table=$(loop_cfg "$policy" metrics_table product_metrics)
key_ref=$(loop_cfg "$policy" metrics_rest_key_op)
# Both go into the request URL, so each is held to a shape that cannot carry a path or a query.
[[ "$base" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/?$ ]] \
  || { echo '{"error":"metrics_rest_url must be an https origin"}'; exit 1; }
[[ "$table" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] \
  || { echo '{"error":"metrics_table must be a plain identifier"}'; exit 1; }

rest() {  # rest <query string> -> the rows array, empty after three failed tries
  local key out i
  if [ -n "${LOOP_METRICS_REST_KEY+x}" ]; then
    key="$LOOP_METRICS_REST_KEY"
  else
    key=$(loop_op_read "$key_ref")
  fi
  [ -z "$key" ] && return 1
  for i in 1 2 3; do
    # The key goes in through stdin so it never appears in the process list.
    out=$(printf 'header = "apikey: %s"\nheader = "Authorization: Bearer %s"\n' "$key" "$key" \
            | curl -s -m 20 --proto '=https' -K - "${base%/}/rest/v1/$table?$1" 2>/dev/null) \
      && echo "$out" | jq -e 'type == "array"' >/dev/null 2>&1 \
      && { echo "$out"; return 0; }
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
  rows=$(rest "select=sample&day=gte.$from") || { echo '{"error":"postgrest unreadable"}'; exit 1; }
  jq -n --argjson r "$rows" --arg q "$query_id" --arg f "$from" \
    '{n: ([$r[].sample | tonumber] | add // 0), from: $f, query_id: $q, captured_at: (now | todateiso8601)}'
  exit 0
  ;;
metric) ;;
*)
  echo "usage: loop-metrics-postgrest.sh metric <key> <from> <to> | events <from> | list" >&2
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

rows=$(rest "select=value,sample&metric=eq.$(jq -rn --arg k "$key" '$k | @uri')&day=gte.$from&day=lte.$to") \
  || { echo '{"error":"postgrest unreadable"}'; exit 1; }

# The aggregation is fixed by the key prefix, the same rule loop-metrics.sh applies in SQL: a count
# sums the value, a rate divides by the sample, anything else is a sample-weighted mean. Postgres
# numerics can arrive as strings, hence tonumber.
jq -n --argjson r "$rows" --arg q "$query_id" --arg k "$key" --arg f "$from" --arg t "$to" '
  [$r[] | {value: (.value | tonumber), sample: (.sample | tonumber)}] as $d
  | ([$d[].sample] | add // 0) as $n
  | {metric: $k,
     value: (if ($k | test("^(hits|count):")) then ([$d[].value] | add // 0)
             elif $n == 0 then 0
             elif ($k | test("^(errors|rate|share):")) then ([$d[].value] | add) / $n
             else ([$d[] | .value * .sample] | add) / $n end),
     n: $n, n_day_max: ([$d[].sample] | max // 0), days: ($d | length),
     window: {start: $f, end: $t}, query_id: $q, captured_at: (now | todateiso8601)}'
