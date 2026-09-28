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
loop_state "$REPO/$RECORDS_DIR"
echo
echo "caps: measuring<=$(loop_cfg "$LOOP_YAML" cap_concurrent_measuring 1)" \
     "new/7d<=$(loop_cfg "$LOOP_YAML" cap_new_hypotheses_per_7d 3)" \
     "merges/day<=$(loop_cfg "$LOOP_YAML" cap_merges_per_day 3)" \
     "min_sample=$(loop_cfg "$LOOP_YAML" cap_min_sample 200)" \
     "budget=\$$(loop_cfg "$LOOP_YAML" cap_tick_budget_usd 8)"
