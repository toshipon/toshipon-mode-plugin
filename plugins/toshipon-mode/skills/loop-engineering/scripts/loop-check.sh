#!/bin/bash
# The agent's only check command. Runs repo_check and every surface check, in the order the gate
# runs them, so green here means the gate also goes green.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 1
logf="$LOG_DIR/check-local.log"
: >"$logf"
if loop_run_checks "$LOOP_YAML" "$logf" 1; then
  echo "checks green"
  exit 0
fi
tail -40 "$logf"
exit 1
