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
in_prompt=$(cat "$prompt" "$here/../references/cycle-prompt-records-store.md" | grep -o '{{[A-Z_]*}}' | sort -u)
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
# a typo in loop.yaml would quietly hand the repo the D1 reader and every metric would read as
# unreadable.
check "no backend means the D1 reader"   "loop-metrics.sh"         "$(loop_metrics_script "")"
check "d1 is the D1 reader"              "loop-metrics.sh"         "$(loop_metrics_script d1)"
check "command has its own reader"       "loop-metrics-command.sh" "$(loop_metrics_script command)"
loop_metrics_script bigquery >/dev/null 2>&1
check "an unknown backend is refused"    "1"                       "$?"

# The deployed version the runner hands the tick for its baseline. It runs deploy_verify from the
# repo, so the tick never needs the command; an absent, failing or null answer must read as empty.
dv="$fixture/deploy"
mkdir -p "$dv/app"
dv_version() {  # dv_version <deploy_verify> -> what loop_deploy_version returns for it
  printf 'deploy_verify: %s\ndeploy_verify_cwd: app\ndeploy_version_jq: .versions[0].version_id\n' "$1" >"$dv/loop.yaml"
  ( REPO="$dv"; loop_deploy_version "$dv/loop.yaml" )
}
check "deploy version is read"            "v-123" "$(dv_version "echo '{\"versions\":[{\"version_id\":\"v-123\"}]}'")"
check "deploy_verify runs in its cwd"     "app"   "$(dv_version "basename \$PWD | jq -R '{versions:[{version_id:.}]}'")"
check "a null version reads as empty"     ""      "$(dv_version "echo '{\"versions\":[]}'")"
check "a failing verify reads as empty"   ""      "$(dv_version "exit 1")"
printf 'loop_id: x\n' >"$dv/loop.yaml"
check "no deploy_verify reads as empty"   ""      "$( REPO="$dv"; loop_deploy_version "$dv/loop.yaml" )"

# The command reader: the repo supplies a script that prints daily rows, the plugin aggregates. The
# aggregation is the contract a hypothesis is scored by, so each prefix is pinned to a number
# worked out by hand.
cr="$fixture/cmd"
mkdir -p "$cr/scripts" "$cr/product" "$cr/state"
cat >"$cr/loop.yaml" <<'YAML'
loop_id: selftest-cmd
metrics_backend: command
metrics_command: scripts/metric-rows.sh
metrics_file: metrics.yaml
allowed_paths:
  - product/
YAML
cat >"$cr/metrics.yaml" <<'YAML'
metrics:
  "count:calls":
    kind: operational
  "rate:failures":
    kind: operational
  "avg_ms:call":
    kind: operational
YAML
cat >"$cr/scripts/metric-rows.sh" <<'SH'
#!/bin/bash
echo "$*" >>"$FAKE_ROWS_LOG"
case "$2" in
  2099-*) echo '[]' ;;
  *) echo '[{"day":"2026-10-01","value":10,"sample":100},{"day":"2026-10-02","value":"30","sample":"300"},{"day":"2026-10-03","value":20,"sample":200}]' ;;
esac
SH
cmd_git() { git -C "$cr" -c user.name=selftest -c user.email=selftest@example.com "$@"; }
cmd_publish() { cmd_git add -A && cmd_git commit -qm "$1" && cmd_git update-ref refs/remotes/origin/main HEAD; }
git -C "$cr" init -q
cmd_publish fixture
cmd_metrics() {
  FAKE_ROWS_LOG="$cr/rows.log" LOOP_REPO="$cr" \
  LOOP_STATE_DIR="$cr/state" LOOP_LOG_DIR="$cr/state" LOOP_BIN_DIR="$cr/state" \
    /bin/bash "$here/loop-metrics-command.sh" "$@" 2>/dev/null
}
out=$(cmd_metrics metric count:calls 2026-10-01 2026-10-03)
check "command count sums the value"        "60"   "$(echo "$out" | jq -r .value)"
check "command n sums the sample"           "600"  "$(echo "$out" | jq -r .n)"
check "command n_day_max is the busiest day" "300" "$(echo "$out" | jq -r .n_day_max)"
check "command counts the days"             "3"    "$(echo "$out" | jq -r .days)"
check "command passes metric and window"    "count:calls 2026-10-01 2026-10-03" "$(tail -1 "$cr/rows.log")"
check "command rate divides by the sample"  "0.1"  "$(cmd_metrics metric rate:failures 2026-10-01 2026-10-03 | jq -r .value)"
# (10*100 + 30*300 + 20*200) / 600
check "command mean is sample-weighted"     "23.3" "$(cmd_metrics metric avg_ms:call 2026-10-01 2026-10-03 | jq -r '.value * 10 | round / 10')"
check "command empty window is zero, not an error" "0" "$(cmd_metrics metric count:calls 2099-01-01 2099-01-02 | jq -r .n)"
check "command events sums every declared metric" "1800" "$(cmd_metrics events 2026-10-01 | jq -r .n)"
check "command refuses an undeclared metric" "count:other is not declared in $cr/metrics.yaml" \
  "$(cmd_metrics metric count:other 2026-10-01 2026-10-03 | jq -r .error)"
check "command lists the declared metrics"  "count:calls rate:failures avg_ms:call" "$(cmd_metrics list | tr '\n' ' ' | sed 's/ *$//')"
# query_id covers the repo's script as well as the reader, so a change to how rows are fetched
# stops judgment on records in flight the same way a change to the reader does.
qid_before=$(echo "$out" | jq -r .query_id)
check "command query_id names both shas" "1" "$(echo "$qid_before" | grep -cE '^m:[0-9a-f]{7}\.[0-9a-f]{7}$')"

# The tick can edit its own worktree. The script that runs is origin/main's, never the worktree's.
cat >"$cr/scripts/metric-rows.sh" <<'SH'
#!/bin/bash
echo '[{"day":"2026-10-01","value":999999,"sample":1}]'
SH
check "command ignores a worktree edit of the script" "60" \
  "$(cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .value)"
cmd_publish "the scoring script changed on main"
check "command query_id moves when the script changes on main" "1" \
  "$([ "$(cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .query_id)" != "$qid_before" ] && echo 1 || echo 0)"

cmd_policy() {  # cmd_policy <label> <sed expression> -> the reader's error with that loop.yaml on origin/main
  sed -i.bak -e "$2" "$cr/loop.yaml" && rm -f "$cr/loop.yaml.bak"
  cmd_publish "$1"
  cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .error
}
# A loop that may edit its own reader writes its own scorecard, whatever origin/main says today.
check "command inside allowed_paths is refused" "metrics_command must be outside allowed_paths" \
  "$(cmd_policy inside 's|^metrics_command:.*|metrics_command: product/metric-rows.sh|')"
check "command path that climbs out of the repo is refused" "metrics_command must be a path inside the repo" \
  "$(cmd_policy climbs 's|^metrics_command:.*|metrics_command: ../outside.sh|')"
check "command missing on origin/main is unreadable" "metrics_command is not readable from origin/main" \
  "$(cmd_policy missing 's|^metrics_command:.*|metrics_command: scripts/absent.sh|')"
sed -i.bak -e 's|^metrics_command:.*|metrics_command: scripts/metric-rows.sh|' "$cr/loop.yaml" && rm -f "$cr/loop.yaml.bak"
printf '#!/bin/bash\necho "not json"\n' >"$cr/scripts/metric-rows.sh"
cmd_publish "a script that prints garbage"
# Garbage is "cannot measure", never "measured zero": a zero would be scored as a real baseline.
check "command output that is not rows is unreadable" "metrics_command did not return rows" \
  "$(cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .error)"
printf '#!/bin/bash\nexit 3\n' >"$cr/scripts/metric-rows.sh"
cmd_publish "a script that fails"
check "command that exits non-zero is unreadable" "metrics_command did not return rows" \
  "$(cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .error)"
cmd_git update-ref -d refs/remotes/origin/main
check "command without origin/main cannot measure" "loop.yaml is not readable from origin/main" \
  "$(cmd_metrics metric count:calls 2026-10-01 2026-10-03 | jq -r .error)"

# Records in an external store. These drive loop-run.sh end to end against a local origin, with
# claude, gh and osascript faked, because what matters is the order the runner does things in: no
# tick without a pull, a push after every tick that ran, and a digest that says when the push failed.
check "one hook alone is refused" "records_pull and records_push must be set together" \
  "$(printf 'records_pull: a.sh\n' >"$fixture/one.yaml"; loop_records_hooks "$fixture/one.yaml")"
check "no hooks means git"        "off" "$(loop_records_hooks "$fixture/loop.yaml")"
printf 'a\n' >"$fixture/j-old"; printf 'a\n- **要約:** x\n' >"$fixture/j-new"
check "journal entry is the working-copy diff" "- **要約:** x" "$(loop_journal_added "$fixture/j-old" "$fixture/j-new")"
check "a journal the pull did not write is all new" "a" "$(loop_journal_added "$fixture/absent" "$fixture/j-old")"

rr="$fixture/run"
mkdir -p "$rr/fakebin" "$rr/store" "$rr/state"
cat >"$rr/fakebin/claude" <<'SH'
#!/bin/bash
printf '%s' "$2" >"$FAKE_DIR/prompt"
touch "$FAKE_DIR/claude-ran"
[ -n "${FAKE_JOURNAL:-}" ] && printf '\n## 2026-10-08 t\n- **要約:** working copy entry\n- **行動:** EVALUATE PH-0001\n' >>product/journal.md
[ -n "${FAKE_SYMLINK:-}" ] && rm -f product/journal.md && ln -s "$FAKE_DIR/secret" product/journal.md
[ -n "${FAKE_HARDLINK:-}" ] && ln "$FAKE_DIR/secret" product/hypotheses/PH-0002.yaml
echo '{"total_cost_usd": 0.5}'
exit "${FAKE_RC:-0}"
SH
printf '#!/bin/bash\nexit 0\n' >"$rr/fakebin/gh"
printf '#!/bin/bash\nexit 0\n' >"$rr/fakebin/osascript"
chmod +x "$rr/fakebin"/*
printf 'id: PH-0001\nstate: measuring\npriority: 1\nmetric: count:x\n' >"$rr/store/PH-0001.yaml"
printf '# journal\n\n## 2026-10-07 old\n- **要約:** pulled entry\n' >"$rr/store/journal.md"

git init -q --bare "$rr/origin.git"
git clone -q "$rr/origin.git" "$rr/main" 2>/dev/null
mkdir -p "$rr/main/product" "$rr/main/scripts" "$rr/main/app"
cat >"$rr/main/scripts/records.sh" <<'SH'
#!/bin/bash
case "$1" in
  pull)
    [ -n "${FAKE_PULL_FAIL:-}" ] && exit 7
    mkdir -p "$3/hypotheses"
    cp "$FAKE_DIR/store/PH-0001.yaml" "$3/hypotheses/"
    cp "$FAKE_DIR/store/journal.md" "$3/journal.md"
    ;;
  push)
    echo "$# $1 $(basename "$4")" >>"$FAKE_DIR/pushes"
    [ -n "${FAKE_PUSH_FAIL:-}" ] && exit 5
    diff "$4/journal.md" "$3/journal.md" >"$FAKE_DIR/pushed.diff"
    exit 0
    ;;
esac
SH
cp "$rr/main/scripts/records.sh" "$rr/main/app/records.sh"
printf 'product/hypotheses/\nproduct/measurements/\nproduct/journal.md\n' >"$rr/main/.gitignore"
run_git() { git -C "$rr/main" -c user.name=selftest -c user.email=selftest@example.com "$@"; }
run_yaml() {  # run_yaml <extra loop.yaml lines> -> publishes loop.yaml on origin/main
  printf 'approved_by: selftest\napproved_at: 2026-10-08\nloop_id: loop-selftest-records\nrecords_dir: product\nbranch_prefix: loop/\nallowed_paths:\n  - app/\n%s' "$1" >"$rr/main/product/loop.yaml"
  run_git add -A && run_git commit -qm "fixture: ${1:-no hooks}" --allow-empty && run_git push -q origin HEAD:main 2>/dev/null
}
run_tick() {  # run_tick -> the runner's stdout; leaves claude-ran, pushes, digest.log in $rr
  rm -f "$rr/claude-ran" "$rr/pushes" "$rr/pushed.diff" "$rr/state/digest.log" "$rr/state/notify.log" \
    "$rr"/state/state-*.txt
  FAKE_DIR="$rr" LOOP_PATH="$rr/fakebin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  LOOP_STATE_DIR="$rr/state" LOOP_LOG_DIR="$rr/state" LOOP_BIN_DIR="$rr/state/bin" \
  LOOP_WORKTREE="$rr/wt" LOOP_SHARED_LOCK="$rr/lock" LOOP_SLACK_WEBHOOK="" \
    /bin/bash "$here/loop-run.sh" "$rr/main" t 2>&1
  echo "rc=$?"
}
ran() { [ -e "$rr/claude-ran" ] && echo yes || echo no; }
pushed() { [ -e "$rr/pushes" ] && cat "$rr/pushes" || echo none; }
hooks=$'records_pull: scripts/records.sh\nrecords_push: scripts/records.sh\n'

run_git checkout -q -b main
run_yaml ""
out=$(run_tick)
check "hooks absent: the tick runs"             "yes"  "$(ran)"
check "hooks absent: nothing is pulled or pushed" "none" "$(pushed)"
check "hooks absent: no pull log"               "0"    "$(ls "$rr/state" | grep -c '^records-pull-')"
check "hooks absent: the prompt has no store section" "0" "$(grep -c '記録は外部の記録面にある' "$rr/prompt")"
check "hooks absent: the digest reads git"      "1"    "$(grep -c '記録を動かさなかった\|tick moved nothing' "$rr/state/digest.log")"

run_yaml $'records_pull: scripts/absent.sh\nrecords_push: scripts/records.sh\n'
out=$(run_tick)
check "pull missing on origin/main is refused" "tick refused: records_pull is not readable from origin/main" \
  "$(echo "$out" | sed -n 's/^\[[^]]*\] //p' | tail -1)"
check "a refused tick does not run"            "no"   "$(ran)"
run_yaml $'records_pull: app/records.sh\nrecords_push: scripts/records.sh\n'
out=$(run_tick)
check "pull inside allowed_paths is refused"   "tick refused: records_pull must be outside allowed_paths" \
  "$(echo "$out" | sed -n 's/^\[[^]]*\] //p' | tail -1)"
run_yaml $'records_pull: scripts/records.sh\nrecords_push: app/records.sh\n'
out=$(run_tick)
check "push inside allowed_paths is refused"   "tick refused: records_push must be outside allowed_paths" \
  "$(echo "$out" | sed -n 's/^\[[^]]*\] //p' | tail -1)"
check "a refused tick pushes nothing"          "none" "$(pushed)"

run_yaml "$hooks"
out=$(FAKE_PULL_FAIL=1 run_tick)
check "a failed pull does not run the tick"    "no"   "$(ran)"
check "a failed pull exits non-zero"           "rc=1" "$(echo "$out" | tail -1)"
check "a failed pull is logged once"           "1"    "$(echo "$out" | grep -c 'tick not run: records_pull exited 7')"
check "a failed pull is notified"              "1"    "$(grep -c 'records_pull exited 7' "$rr/state/notify.log")"
check "a failed pull pushes nothing"           "none" "$(pushed)"

out=$(FAKE_JOURNAL=1 run_tick)
check "hooks: the tick runs after the pull"    "yes"  "$(ran)"
check "hooks: the tick sees the pulled records" "1"   "$(grep -c '^PH-0001  measuring' "$rr"/state/state-*.txt)"
check "hooks: the prompt has the store section" "1"   "$(grep -c '記録は外部の記録面にある' "$rr/prompt")"
check "hooks: push gets verb, worktree, records, pulled copy" "4 push pulled" "$(pushed)"
check "hooks: the journal digest is the working-copy diff" "1" "$(grep -c '^- \*\*要約:\*\* working copy entry' "$rr/state/digest.log")"
check "hooks: the pulled entry is not in the digest" "0" "$(grep -c 'pulled entry' "$rr/state/digest.log")"
check "hooks: the push saw the new entry"      "1"    "$(grep -c 'working copy entry' "$rr/pushed.diff")"
check "hooks: records are not committed"       ""     "$(git -C "$rr/wt" status --porcelain)"

out=$(FAKE_JOURNAL=1 FAKE_RC=1 run_tick)
check "a failed agent run is still pushed"     "4 push pulled" "$(pushed)"
check "a failed agent run keeps its rc"        "rc=1" "$(echo "$out" | tail -1)"

out=$(FAKE_JOURNAL=1 FAKE_PUSH_FAIL=1 run_tick)
check "a failed push says NOT saved in the digest" "1" "$(grep -c 'records NOT saved' "$rr/state/digest.log")"
check "a failed push is notified"              "1"    "$(grep -c 'records NOT saved: records_push exited 5' "$rr/state/notify.log")"
check "a failed push keeps the working copy"   "1"    "$(grep -c 'working copy entry' "$rr"/state/records-unsaved-*/journal.md | tail -1 | sed 's/.*://')"
check "a failed push does not fail the tick"   "rc=0" "$(echo "$out" | tail -1)"

# The push sends files the agent could edit off the machine. A record swapped for a link to a secret
# would carry the secret out, through the push or through the digest the runner posts.
printf '# journal\n- **要約:** SECRET-TOKEN\n' >"$rr/secret"
out=$(FAKE_SYMLINK=1 run_tick)
check "a symlinked record is not pushed"       "none" "$(pushed)"
check "a symlinked record says NOT saved"      "1"    "$(grep -c 'records NOT saved' "$rr/state/digest.log")"
check "a symlinked record is notified"         "1"    "$(grep -c 'not a plain file: .*product/journal.md' "$rr/state/notify.log")"
check "a symlinked record never reaches the digest" "0" "$(cat "$rr/state/digest.log" "$rr/state/notify.log" | grep -c SECRET-TOKEN)"
out=$(run_tick)
check "a symlink left behind stops the next tick" "no" "$(ran)"
check "a symlink left behind is not pulled through" "1" "$(grep -c SECRET-TOKEN "$rr/secret")"
rm -f "$rr/wt/product/journal.md"
out=$(FAKE_HARDLINK=1 run_tick)
check "a hard-linked record is not pushed"     "none" "$(pushed)"
check "a hard-linked record says NOT saved"    "1"    "$(grep -c 'records NOT saved' "$rr/state/digest.log")"
rm -f "$rr/wt/product/hypotheses/PH-0002.yaml"
out=$(run_tick)
check "a clean records dir is pushed again"    "4 push pulled" "$(pushed)"

printf 'product/hypotheses/\n' >"$rr/main/.gitignore"
run_yaml "$hooks"
out=$(run_tick)
check "records git can see refuse the tick"    "no"   "$(ran)"
check "records git can see are named"          "1"    "$(echo "$out" | grep -c 'git does not ignore: ?? product/journal.md')"
printf 'product/hypotheses/\nproduct/measurements/\nproduct/journal.md\n' >"$rr/main/.gitignore"
touch "$rr/main/product/paused.flag"
run_yaml "$hooks"
out=$(run_tick)
check "a paused tick does not run"             "no"   "$(ran)"
check "a paused tick pushes nothing"           "none" "$(pushed)"

out=$(FAKE_DIR="$rr" LOOP_PATH="$rr/fakebin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
  LOOP_STATE_DIR="$rr/state" LOOP_LOG_DIR="$rr/state" LOOP_BIN_DIR="$rr/state/bin" \
  /bin/bash "$here/loop-status.sh" "$rr/main" 2>&1)
check "status reads the store through the pull" "PH-0001  measuring  priority=1  metric=count:x" \
  "$(echo "$out" | grep '^PH-0001')"
check "status keeps git's paused.flag"          "1" "$(echo "$out" | grep -c '^PAUSED:')"

echo
if [ "$fails" -eq 0 ]; then
  echo "loop-selftest: all checks passed under $BASH_VERSION"
  exit 0
fi
echo "loop-selftest: $fails check(s) failed under $BASH_VERSION"
exit 1
