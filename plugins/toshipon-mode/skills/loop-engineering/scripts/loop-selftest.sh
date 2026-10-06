#!/bin/bash
# Drives the pure functions in loop-config.sh against fixtures. Run it with /bin/bash, which is 3.2
# on macOS and is what launchd uses, so an incompatibility shows up here rather than at 15:30.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"

fails=0
check() {  # check <label> <expected> <actual>
  if [ "$2" = "$3" ]; then
    echo "ok   $1"
  else
    echo "FAIL $1: expected [$2], got [$3]"
    fails=$((fails + 1))
  fi
}
check_allowed() {  # check_allowed <label> <yes|no> <path> <entry>...
  local label="$1" want="$2" path="$3"
  shift 3
  if loop_path_allowed "$path" "$@"; then check "$label" "$want" yes; else check "$label" "$want" no; fi
}

fixture=$(mktemp -d)
trap 'rm -rf "$fixture"' EXIT

cat >"$fixture/loop.yaml" <<'YAML'
loop_id: selftest
branch_prefix: loop/product-
repo_check: make check   # trailing comment must not survive
quoted_value: "spaces and : colon"
empty_value:
allowed_paths:
  - product/
  - "cloud/app/src/index.ts"
  - docs/loop.md          # comment after a list item
surface_checks:
  - cd cloud/app && npx tsc --noEmit
cap_min_sample: 200
YAML

check "scalar"            "selftest"              "$(loop_cfg "$fixture/loop.yaml" loop_id)"
check "scalar with slash" "loop/product-"         "$(loop_cfg "$fixture/loop.yaml" branch_prefix)"
check "trailing comment"  "make check"            "$(loop_cfg "$fixture/loop.yaml" repo_check)"
check "quoted value"      "spaces and : colon"    "$(loop_cfg "$fixture/loop.yaml" quoted_value)"
check "empty uses default" "fallback"             "$(loop_cfg "$fixture/loop.yaml" empty_value fallback)"
check "absent uses default" "8"                   "$(loop_cfg "$fixture/loop.yaml" cap_tick_budget_usd 8)"
check "number"            "200"                   "$(loop_cfg "$fixture/loop.yaml" cap_min_sample)"

check "list length"       "3"                     "$(loop_cfg_list "$fixture/loop.yaml" allowed_paths | wc -l | tr -d ' ')"
check "list quoted item"  "cloud/app/src/index.ts" "$(loop_cfg_list "$fixture/loop.yaml" allowed_paths | sed -n 2p)"
check "list item comment" "docs/loop.md"          "$(loop_cfg_list "$fixture/loop.yaml" allowed_paths | sed -n 3p)"
check "list stops at next key" "cd cloud/app && npx tsc --noEmit" "$(loop_cfg_list "$fixture/loop.yaml" surface_checks)"

ALLOW=(product/ cloud/app/src/index.ts docs/loop.md)
check_allowed "dir entry covers a file under it"  yes "product/hypotheses/PH-0001.yaml" "${ALLOW[@]}"
check_allowed "dir entry covers itself"           yes "product/"                       "${ALLOW[@]}"
check_allowed "exact entry matches"               yes "cloud/app/src/index.ts"         "${ALLOW[@]}"
check_allowed "exact entry is not a prefix"       no  "cloud/app/src/index.ts.bak"     "${ALLOW[@]}"
check_allowed "sibling with a shared prefix"      no  "products/hypotheses/x.yaml"     "${ALLOW[@]}"
check_allowed "parent of a dir entry"             no  "product.yaml"                   "${ALLOW[@]}"
check_allowed "unrelated path"                    no  "cloud/live/src/index.ts"        "${ALLOW[@]}"
check_allowed "empty allowlist refuses"           no  "product/hypotheses/PH-0001.yaml"
# The three files the design must never let the loop reach, against a realistic allowlist.
REAL=(product/hypotheses/ product/journal.md cloud/paper-trader/src/index.ts cloud/paper-trader/src/views/)
check_allowed "loop.yaml stays out"      no "product/loop.yaml"                        "${REAL[@]}"
check_allowed "metrics.yaml stays out"   no "product/metrics.yaml"                     "${REAL[@]}"
check_allowed "scoring code stays out"   no "cloud/paper-trader/src/analytics.ts"      "${REAL[@]}"
check_allowed "auth code stays out"      no "cloud/paper-trader/src/auth/zero_trust.ts" "${REAL[@]}"

records="$fixture/records"
mkdir -p "$records/hypotheses"
for spec in "PH-0001 measuring 1 avg_ms:/cron/mtm" "PH-0002 drafted 2 errors:/cron/mtm" \
            "PH-0003 inconclusive 3 hits:/" "PH-0004 validated 4 avg_ms:/"; do
  set -- $spec
  cat >"$records/hypotheses/$1.yaml" <<YAML
id: $1
state: $2
priority: $3
metric: $4
YAML
done
REPO="$fixture" state=$(loop_state "$records")
check "state total"      "records: 4"    "$(echo "$state" | sed -n 1p)"
check "state measuring"  "  measuring: 1" "$(echo "$state" | grep '^  measuring:')"
check "state drafted"    "  drafted: 1"   "$(echo "$state" | grep '^  drafted:')"
check "theatre ratio"    "theatre check: 1 of 2 terminal records are inconclusive (50%)" \
                         "$(echo "$state" | grep '^theatre check:')"
check "record line"      "PH-0002  drafted  priority=2  metric=errors:/cron/mtm" \
                         "$(echo "$state" | grep '^PH-0002')"
touch "$records/paused.flag"
check "paused is reported" "PAUSED: $records/paused.flag exists" \
                           "$(REPO="$fixture" loop_state "$records" | grep '^PAUSED:')"
rm -f "$records/hypotheses"/*.yaml "$records/paused.flag"
check "empty records dir" "theatre check: no terminal records yet" \
                          "$(REPO="$fixture" loop_state "$records" | grep '^theatre check:')"

check_health() {  # check_health <label> <expected reason or empty> <health command path>
  local label="$1" want="$2" cmd="$3" got
  got=$(loop_health "$fixture" "$cmd") || true
  check "$label" "$want" "$got"
}
mkdir -p "$fixture/bin"
printf '#!/bin/bash\necho "equity 100 floor 83"\n' >"$fixture/bin/healthy.sh"
printf '#!/bin/bash\necho "anomalies: below_capital_floor_trading_halted"\n' >"$fixture/bin/sick.sh"
printf '#!/bin/bash\necho "d1 unreadable" >&2\nexit 1\n' >"$fixture/bin/broken.sh"
printf '#!/bin/bash\necho hi\n' >"$fixture/bin/notexec.sh"
chmod +x "$fixture/bin/healthy.sh" "$fixture/bin/sick.sh" "$fixture/bin/broken.sh"
check_health "no health command configured is a pass" "" ""
check_health "a healthy command is a pass"            "" "bin/healthy.sh"
check_health "an anomaly is refused"                  "deploy_health reports a problem: anomalies: below_capital_floor_trading_halted" "bin/sick.sh"
check_health "a non-zero exit is refused"             "deploy_health exited non-zero: d1 unreadable" "bin/broken.sh"
check_health "a non-executable command is refused"    "deploy_health is set to bin/notexec.sh but it is not executable here" "bin/notexec.sh"
check_health "a missing command is refused"           "deploy_health is set to bin/absent.sh but it is not executable here" "bin/absent.sh"

# Slack must be optional. A loop with no webhook configured has to run exactly as before, so the
# disabled path is the one worth pinning: set-but-empty posts nothing and still returns success.
LOOP_SLACK_WEBHOOK="" loop_slack "selftest must not post this" && check "an empty webhook posts nothing" "0" "0" \
  || check "an empty webhook posts nothing" "0" "1"
( unset LOOP_SLACK_WEBHOOK; LOOP_YAML=/dev/null loop_slack "selftest must not post this" ) \
  && check "no webhook configured is a pass" "0" "0" || check "no webhook configured is a pass" "0" "1"

check_pathsafe() {  # check_pathsafe <label> <expected> <path>
  local label="$1" want="$2" got
  got=$(loop_pathsafe "$3") || true
  check "$label" "$want" "$got"
}
check_pathsafe "a plain path is usable"   "" "/Users/x/.cache/loop/bin"
# "Application Support" is the trap: the gate paths went into --allowedTools and matched nothing,
# so every gate call was denied and the tick burned budget discovering it.
check_pathsafe "a path with a space is refused" \
  "path contains whitespace and cannot be used in a tool permission: /Users/x/Library/Application Support/l/bin" \
  "/Users/x/Library/Application Support/l/bin"
check_pathsafe "a tab is refused too" \
  "path contains whitespace and cannot be used in a tool permission: /Users/x/a	b" "/Users/x/a	b"

check_lock() {  # check_lock <label> <expected> <lock path>
  local label="$1" want="$2" got
  got=$(loop_lock "$3") || true
  check "$label" "$want" "$got"
}
check_lock "a fresh lock is taken"          "held" "$fixture/locks/a/cycle.lock"
check_lock "the same lock is then busy"     "busy: another cycle holds $fixture/locks/a/cycle.lock" "$fixture/locks/a/cycle.lock"
# A missing parent used to be reported as "another cycle holds it", so a misconfigured loop looked
# like a patient one and skipped every tick forever.
printf 'not a directory\n' >"$fixture/afile"
check_lock "a path under a file is unusable" "unusable: cannot create the lock at $fixture/afile/cycle.lock" "$fixture/afile/cycle.lock"
check_lock "a deep new parent is created"    "held" "$fixture/locks/b/c/d/cycle.lock"

# An unsubstituted placeholder does not fail loudly. The tick just receives the literal text and
# silently cannot measure, check, push or merge, and the journal reads like a quiet cycle.
prompt="$here/../references/cycle-prompt.md"
in_prompt=$(grep -o '{{[A-Z_]*}}' "$prompt" | sort -u)
in_runner=$(grep -o 's|{{[A-Z_]*}}' "$here/loop-run.sh" | sed 's/^s|//' | sort -u)
check "every prompt placeholder is substituted" "" "$(comm -23 <(echo "$in_prompt") <(echo "$in_runner") | tr '\n' ' ' | sed 's/ *$//')"
check "the runner substitutes nothing unused" "" "$(comm -13 <(echo "$in_prompt") <(echo "$in_runner") | tr '\n' ' ' | sed 's/ *$//')"

# A denied Bash call is not an error the tick reports. It lands in permission_denials in the run
# JSON, the agent quietly tries something else, and the turns are gone. The first real loop lost
# two turns a tick to `cat a; echo ---; cat b`, because a compound command needs EVERY part
# allowed and `echo` was missing. So assert the read-only set the agent actually reaches for.
allowed=$(sed -n '/--allowedTools/,/--disallowedTools/p' "$here/loop-run.sh")
missing=""
for c in cd ls cat head tail grep jq wc date mkdir echo; do
  echo "$allowed" | grep -q "\"Bash($c:\*)\"" || missing="$missing $c"
done
check "every read-only command the tick uses is allowed" "" "${missing# }"

# Telemetry SQL is built by string concatenation, so an apostrophe in a host name or a branch would
# otherwise end the literal early. These two are the only escaping in the loop.
check "a plain value is quoted"      "'daily'"        "$(loop_sql_str daily)"
check "an apostrophe is doubled"     "'toshipon''s Mac'" "$(loop_sql_str "toshipon's Mac")"
check "an empty value becomes NULL"  "NULL"           "$(loop_sql_str "")"
check "a number passes through"      "1.06"           "$(loop_sql_num 1.06)"
check "a non-number becomes NULL"    "NULL"           "$(loop_sql_num '?')"
check "an empty number becomes NULL" "NULL"           "$(loop_sql_num "")"
# Without status_db the whole feature is a no-op, so a repo that has not opted in still ticks.
LOOP_YAML="$fixture/loop.yaml" REPO="$fixture" LOOP_ID=selftest
check "no status_db writes nothing"  ""               "$(loop_status_exec "SELECT 1")"
check "no status_db reads nothing"   ""               "$(loop_status_summary)"
# "not configured" must be distinguishable from "configured but no rows yet", or a silent loop and
# an unmeasured loop print the same line.
loop_status_summary >/dev/null 2>&1
check "no status_db reports rc 1"    "1"              "$?"

# The backend picks which reader the tick gets as loop-metrics.sh. An unknown name must refuse, or
# a typo in loop.yaml would quietly hand a Postgres repo the D1 reader and every metric would read
# as unreadable.
check "no backend means the D1 reader"   "loop-metrics.sh"           "$(loop_metrics_script "")"
check "d1 is the D1 reader"              "loop-metrics.sh"           "$(loop_metrics_script d1)"
check "postgrest has its own reader"     "loop-metrics-postgrest.sh" "$(loop_metrics_script postgrest)"
loop_metrics_script bigquery >/dev/null 2>&1
check "an unknown backend is refused"    "1"                         "$?"

# The PostgREST reader against a canned response. The aggregation is the contract a hypothesis is
# scored by, so each prefix is pinned to a number worked out by hand.
pg="$fixture/pg"
mkdir -p "$pg/fakebin" "$pg/state"
cat >"$pg/loop.yaml" <<'YAML'
loop_id: selftest-pg
metrics_backend: postgrest
metrics_rest_url: https://example.supabase.co
metrics_table: product_metrics
metrics_file: metrics.yaml
YAML
cat >"$pg/metrics.yaml" <<'YAML'
metrics:
  "count:calls":
    kind: operational
  "rate:failures":
    kind: operational
  "avg_ms:call":
    kind: operational
YAML
cat >"$pg/fakebin/curl" <<'SH'
#!/bin/bash
for a in "$@"; do url="$a"; done
echo "$url" >>"$FAKE_CURL_LOG"
cat >/dev/null
case "$url" in
  *metric=eq.*) echo '[{"value":10,"sample":100},{"value":"30","sample":"300"},{"value":20,"sample":200}]' ;;
  *day=gte.2099*) echo '[]' ;;
  *) echo '[{"sample":100},{"sample":300}]' ;;
esac
SH
chmod +x "$pg/fakebin/curl"
pg_metrics() {
  FAKE_CURL_LOG="$pg/curl.log" LOOP_REPO="$pg" LOOP_METRICS_REST_KEY=test-key \
  LOOP_PATH="$pg/fakebin:$PATH" LOOP_STATE_DIR="$pg/state" LOOP_LOG_DIR="$pg/state" LOOP_BIN_DIR="$pg/state" \
    /bin/bash "$here/loop-metrics-postgrest.sh" "$@" 2>/dev/null
}
out=$(pg_metrics metric count:calls 2026-10-01 2026-10-03)
check "postgrest count sums the value"       "60"   "$(echo "$out" | jq -r .value)"
check "postgrest n sums the sample"          "600"  "$(echo "$out" | jq -r .n)"
check "postgrest n_day_max is the busiest day" "300" "$(echo "$out" | jq -r .n_day_max)"
check "postgrest counts the days"            "3"    "$(echo "$out" | jq -r .days)"
check "postgrest query_id is the reader's own sha" \
  "m:$(shasum -a 256 "$here/loop-metrics-postgrest.sh" | cut -c1-7)" "$(echo "$out" | jq -r .query_id)"
check "postgrest rate divides by the sample" "0.1"  "$(pg_metrics metric rate:failures 2026-10-01 2026-10-03 | jq -r .value)"
# (10*100 + 30*300 + 20*200) / 600
check "postgrest mean is sample-weighted"    "23.3" "$(pg_metrics metric avg_ms:call 2026-10-01 2026-10-03 | jq -r '.value * 10 | round / 10')"
check "postgrest filters by metric and window" \
  "https://example.supabase.co/rest/v1/product_metrics?select=value,sample&metric=eq.avg_ms%3Acall&day=gte.2026-10-01&day=lte.2026-10-03" \
  "$(tail -1 "$pg/curl.log")"
check "postgrest events sums every sample"   "400"  "$(pg_metrics events 2026-10-01 | jq -r .n)"
check "postgrest empty window is zero, not an error" "0" "$(pg_metrics events 2099-01-01 | jq -r .n)"
check "postgrest refuses an undeclared metric" "count:other is not declared in $pg/metrics.yaml" \
  "$(pg_metrics metric count:other 2026-10-01 2026-10-03 | jq -r .error)"
check "postgrest lists the declared metrics" "count:calls rate:failures avg_ms:call" "$(pg_metrics list | tr '\n' ' ' | sed 's/ *$//')"
# No key is "cannot measure", never "measured zero": a zero would be scored as a real baseline.
check "postgrest without a key is unreadable" "postgrest unreadable" \
  "$(LOOP_METRICS_REST_KEY="" pg_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .error)"

echo
if [ "$fails" -eq 0 ]; then
  echo "loop-selftest: all checks passed under $BASH_VERSION"
  exit 0
fi
echo "loop-selftest: $fails check(s) failed under $BASH_VERSION"
exit 1
