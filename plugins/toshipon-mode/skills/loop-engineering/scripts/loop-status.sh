#!/bin/bash
# loop-status.sh <main-repo>
# Prints the board: record counts by state, the theatre ratio, and one line per record.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
loop_env "${1:?repo path required}" || exit 1
echo "loop: $LOOP_ID"
echo "records_dir: $RECORDS_DIR"
echo "branch_prefix: $BRANCH_PREFIX"
echo
# With records hooks the records are not in git, and the main checkout's copy is whatever the last
# pull left there, if anything. Pull a fresh one into a scratch dir with origin/main's script.
policy=$(mktemp)
board=""
trap 'rm -f "$policy"; [ -z "$board" ] || rm -rf "$board"' EXIT
git -C "$REPO" fetch -q origin main 2>/dev/null
git -C "$REPO" show "origin/main:${LOOP_YAML#"$REPO"/}" >"$policy" 2>/dev/null || cp "$LOOP_YAML" "$policy"
if ! hooks=$(loop_records_hooks "$policy"); then
  echo "records: $hooks"
elif [ "$hooks" = on ]; then
  board=$(mktemp -d) && mkdir -p "$board/records"
  if ! reason=$(loop_repo_script "$REPO" "$policy" records_pull "$board/pull.sh"); then
    echo "records: $reason"
  elif ! (cd "$REPO" && /bin/bash "$board/pull.sh" pull "$REPO" "$board/records") >"$board/pull.log" 2>&1; then
    echo "records: records_pull failed: $(loop_oneline "$(cat "$board/pull.log")")"
  else
    [ -f "$REPO/$RECORDS_DIR/paused.flag" ] && cp "$REPO/$RECORDS_DIR/paused.flag" "$board/records/"
    loop_state "$board/records"
  fi
else
  loop_state "$REPO/$RECORDS_DIR"
fi
echo
echo "caps: measuring<=$(loop_cfg "$LOOP_YAML" cap_concurrent_measuring 1)" \
     "new/7d<=$(loop_cfg "$LOOP_YAML" cap_new_hypotheses_per_7d 3)" \
     "merges/day<=$(loop_cfg "$LOOP_YAML" cap_merges_per_day 3)" \
     "min_sample=$(loop_cfg "$LOOP_YAML" cap_min_sample 200)" \
     "budget=\$$(loop_cfg "$LOOP_YAML" cap_tick_budget_usd 8)"

# Whether the loop is running at all, and from where. A tick that ends in WAIT leaves nothing in
# git, so "is it alive" cannot be answered from the repo.
echo
if status=$(loop_status_summary); then
  echo "$status"
else
  echo "liveness: status_db is not configured (a WAIT tick leaves no trace in git)"
fi
