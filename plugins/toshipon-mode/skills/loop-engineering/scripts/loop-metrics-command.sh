#!/bin/bash
# loop-metrics-command.sh metric <key> <from YYYY-MM-DD> <to YYYY-MM-DD>
# loop-metrics-command.sh events <from YYYY-MM-DD>
# loop-metrics-command.sh list
#
# The same contract as loop-metrics.sh, for a repo whose rollup is not in D1. The plugin does not
# learn the repo's datastore: loop.yaml names a script (metrics_command), the script prints the
# daily rows for one metric, and the aggregation stays here. loop-run.sh installs this file as the
# tick's loop-metrics.sh when loop.yaml says metrics_backend: command.
#
#   <metrics_command> <metric> <from> <to>  ->  [{"day": "...", "value": n, "sample": n}, ...]
#
# Two things keep the loop from writing its own scorecard now that part of the reader is repo code.
# The script that runs is origin/main's copy, never the worktree's, and it must sit outside
# allowed_paths. query_id carries the script's sha next to this file's, so changing how rows are
# fetched stops judgment on records in flight exactly as changing the aggregation does.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 1
fail() { jq -cn --arg e "$1" '{error: $e}'; exit 1; }

metrics_file="$REPO/$(loop_cfg "$LOOP_YAML" metrics_file "$RECORDS_DIR/metrics.yaml")"

work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
git -C "$REPO" show "origin/main:${LOOP_YAML#"$REPO"/}" >"$work/loop.yaml" 2>/dev/null \
  || fail "loop.yaml is not readable from origin/main"
command_path=$(loop_cfg "$work/loop.yaml" metrics_command)
case "$command_path" in ""|/*|*..*) fail "metrics_command must be a path inside the repo" ;; esac
[[ "$command_path" =~ ^[A-Za-z0-9._/-]+$ ]] || fail "metrics_command must be a path inside the repo"
allowed=()
while IFS= read -r line; do allowed+=("$line"); done < <(loop_cfg_list "$work/loop.yaml" allowed_paths)
loop_path_allowed "$command_path" ${allowed[@]+"${allowed[@]}"} \
  && fail "metrics_command must be outside allowed_paths"
git -C "$REPO" show "origin/main:$command_path" >"$work/rows.sh" 2>/dev/null \
  || fail "metrics_command is not readable from origin/main"

query_id="m:$(shasum -a 256 "$0" | cut -c1-7).$(shasum -a 256 "$work/rows.sh" | cut -c1-7)"

declared() { sed -n 's/^  "\{0,1\}\([^":]*:\{0,1\}[^":]*\)"\{0,1\}:[[:space:]]*$/\1/p' "$metrics_file"; }

rows() {  # rows <metric> <from> <to> -> [{value, sample}], or fails when the script did not answer
  local out
  out=$(cd "$REPO" && /bin/bash "$work/rows.sh" "$1" "$2" "$3" 2>/dev/null) || return 1
  # A row that is not two numbers is "cannot measure". Coercing it to zero would score as a baseline.
  echo "$out" | jq -ce 'if type == "array"
    then map({value: (.value | tonumber), sample: (.sample | tonumber)})
    else error("not rows") end' 2>/dev/null
}

case "${1:-}" in
list)
  declared
  exit 0
  ;;
events)
  from="${2:?from date required}"
  [[ "$from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "bad date"
  to=$(date -u +%Y-%m-%d)
  n=0
  while IFS= read -r key; do
    [ -z "$key" ] && continue
    r=$(rows "$key" "$from" "$to") || fail "metrics_command did not return rows"
    n=$(jq -n --argjson r "$r" --argjson n "$n" '$n + ([$r[].sample] | add // 0)')
  done < <(declared)
  jq -n --argjson n "$n" --arg q "$query_id" --arg f "$from" \
    '{n: $n, from: $f, query_id: $q, captured_at: (now | todateiso8601)}'
  exit 0
  ;;
metric) ;;
*)
  echo "usage: loop-metrics-command.sh metric <key> <from> <to> | events <from> | list" >&2
  exit 2
  ;;
esac

key="${2:?metric key required}"
from="${3:?from date required}"
to="${4:?to date required}"
[[ "$key" =~ ^[a-z_]+:?[A-Za-z0-9/._-]*$ ]] || fail "illegal metric key"
[[ "$from" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ && "$to" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || fail "bad date"
grep -qF "\"$key\":" "$metrics_file" || grep -qE "^  $key:" "$metrics_file" \
  || fail "$key is not declared in $metrics_file"

r=$(rows "$key" "$from" "$to") || fail "metrics_command did not return rows"

# The aggregation is fixed by the key prefix, the same rule loop-metrics.sh applies in SQL: a count
# sums the value, a rate divides by the sample, anything else is a sample-weighted mean.
jq -n --argjson d "$r" --arg q "$query_id" --arg k "$key" --arg f "$from" --arg t "$to" '
  ([$d[].sample] | add // 0) as $n
  | {metric: $k,
     value: (if ($k | test("^(hits|count):")) then ([$d[].value] | add // 0)
             elif $n == 0 then 0
             elif ($k | test("^(errors|rate|share):")) then ([$d[].value] | add) / $n
             else ([$d[] | .value * .sample] | add) / $n end),
     n: $n, n_day_max: ([$d[].sample] | max // 0), days: ($d | length),
     window: {start: $f, end: $t}, query_id: $q, captured_at: (now | todateiso8601)}'
