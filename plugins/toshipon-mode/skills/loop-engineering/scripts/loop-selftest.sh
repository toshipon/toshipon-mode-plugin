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

echo
if [ "$fails" -eq 0 ]; then
  echo "loop-selftest: all checks passed under $BASH_VERSION"
  exit 0
fi
echo "loop-selftest: $fails check(s) failed under $BASH_VERSION"
exit 1
