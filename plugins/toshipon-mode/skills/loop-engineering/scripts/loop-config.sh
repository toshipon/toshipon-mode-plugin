# Sourced by every loop script. loop.yaml is flat on purpose: a nested schema needs a YAML
# parser, and launchd starts these scripts with a bare PATH that has none on it.
# Machine-specific values live in ~/.config/loop-engineering/<loop_id>.env, outside the repo.

loop_cfg() {  # loop_cfg <file> <key> [default]
  local v
  v=$(sed -n "s/^$2:[[:space:]]*//p" "$1" 2>/dev/null | head -1 \
        | sed -e 's/[[:space:]]*#.*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'\$/\1/" -e 's/[[:space:]]*$//')
  if [ -n "$v" ]; then echo "$v"; else echo "${3-}"; fi
}

loop_cfg_list() {  # loop_cfg_list <file> <key> -> one item per line
  awk -v key="$2:" '
    $0 == key { inside = 1; next }
    inside && /^[[:space:]]+-[[:space:]]/ {
      sub(/^[[:space:]]*-[[:space:]]*/, "")
      sub(/[[:space:]]+#.*$/, ""); sub(/[[:space:]]*$/, "")
      gsub(/^"|"$/, ""); gsub(/^'\''|'\''$/, "")
      if ($0 != "") print
      next
    }
    inside && /^[^[:space:]]/ { inside = 0 }
  ' "$1" 2>/dev/null
}

# loop_env <repo> [loop-yaml-path]
# Exports REPO, LOOP_YAML, RECORDS_DIR, LOOP_ID, BRANCH_PREFIX, STATE_DIR, LOG_DIR.
loop_env() {
  export REPO="${1:?repo path required}"
  local candidate found=""
  for candidate in "${2:-}" product/loop.yaml docs/loop.yaml loop.yaml; do
    [ -n "$candidate" ] && [ -f "$REPO/$candidate" ] && { found="$candidate"; break; }
  done
  [ -z "$found" ] && { echo "loop.yaml not found under $REPO" >&2; return 1; }
  export LOOP_YAML="$REPO/$found"
  RECORDS_DIR="$(loop_cfg "$LOOP_YAML" records_dir "$(dirname "$found")")"; export RECORDS_DIR
  LOOP_ID="$(loop_cfg "$LOOP_YAML" loop_id "$(basename "$REPO")-loop")"; export LOOP_ID
  BRANCH_PREFIX="$(loop_cfg "$LOOP_YAML" branch_prefix "loop/")"; export BRANCH_PREFIX
  # shellcheck disable=SC1090
  [ -f "$HOME/.config/loop-engineering/$LOOP_ID.env" ] && source "$HOME/.config/loop-engineering/$LOOP_ID.env"
  export PATH="${LOOP_PATH:-$HOME/.local/bin:$HOME/.anyenv/envs/nodenv/shims:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin}"
  export STATE_DIR="${LOOP_STATE_DIR:-$HOME/Library/Application Support/$LOOP_ID}"
  export LOG_DIR="${LOOP_LOG_DIR:-$HOME/Library/Logs/$LOOP_ID}"
  # Deliberately not under STATE_DIR. The runner names this directory inside --allowedTools
  # patterns, and "Application Support" has a space in it, which matches nothing.
  export BIN_DIR="${LOOP_BIN_DIR:-$HOME/.cache/loop-engineering/$LOOP_ID/bin}"
  mkdir -p "$STATE_DIR" "$LOG_DIR" "$BIN_DIR"
}

# loop_slack <text>
# Posts to the loop's Slack webhook, or does nothing when none is configured. The webhook comes from
# LOOP_SLACK_WEBHOOK, or from the op:// reference in loop.yaml read through the 1Password service
# account in the login Keychain. Set but empty disables Slack, which is what the tests rely on.
loop_slack() {
  local url ref
  if [ -n "${LOOP_SLACK_WEBHOOK+x}" ]; then
    url="$LOOP_SLACK_WEBHOOK"
  else
    ref=$(loop_cfg "${LOOP_YAML:-/dev/null}" slack_webhook_op)
    [ -z "$ref" ] && return 0
    url=$(OP_SERVICE_ACCOUNT_TOKEN="$(security find-generic-password -s OP_SERVICE_ACCOUNT_TOKEN -w 2>/dev/null)" \
      op read "$ref" 2>/dev/null)
  fi
  [ -z "$url" ] && return 0
  curl -s -m 15 -X POST -H 'Content-type: application/json' \
    --data "$(jq -n --arg t "$1" '{text: $t}')" "$url" >/dev/null 2>&1 || true
}

loop_notify() {  # loop_notify <title> <message>
  osascript -e "display notification \"${2//\"/\\\"}\" with title \"${1//\"/\\\"}\"" >/dev/null 2>&1 || true
  echo "[$(date -u +%FT%TZ)] $1: $2" >>"$LOG_DIR/notify.log"
  loop_slack "*$1*: $2"
}

# loop_path_allowed <path> <allowlist entry>...
# An entry ending in "/" covers everything under it; anything else must match exactly. This is the
# single most consequential function here, so it lives in one place and loop-selftest.sh drives it.
loop_path_allowed() {
  local path="$1" entry
  shift
  for entry in "$@"; do
    case "$entry" in
      */) [ "${path#"$entry"}" != "$path" ] && return 0 ;;
      *)  [ "$path" = "$entry" ] && return 0 ;;
    esac
  done
  return 1
}

loop_oneline() { echo "$1" | tail -3 | tr '\n' ' ' | sed 's/[[:space:]]*$//'; }

# loop_health <repo> <health-command-path>
# Prints a reason and returns non-zero when the merge must not proceed. A configured command that
# cannot run is not a pass: treating its failure as silence is fail-open, and the loop would merge
# straight through an outage it never saw.
loop_health() {
  local repo="$1" cmd="$2" out
  [ -z "$cmd" ] && return 0
  [ -x "$repo/$cmd" ] || { echo "deploy_health is set to $cmd but it is not executable here"; return 1; }
  if ! out=$("$repo/$cmd" 2>&1); then
    echo "deploy_health exited non-zero: $(loop_oneline "$out")"
    return 1
  fi
  if echo "$out" | grep -qiE 'anomal|halt|stale'; then
    echo "deploy_health reports a problem: $(loop_oneline "$out")"
    return 1
  fi
  return 0
}

# loop_pathsafe <path>
# A path that goes into an --allowedTools pattern must contain no whitespace. A space there matches
# nothing, every gate call is denied, and the tick spends its budget discovering that.
loop_pathsafe() {
  case "$1" in
    *[[:space:]]*) echo "path contains whitespace and cannot be used in a tool permission: $1"; return 1 ;;
    *) return 0 ;;
  esac
}

# loop_lock <lock-dir>
# Takes the lock, or prints why it could not. A held lock and an unusable path are different
# answers: the first means wait, the second means the loop is misconfigured and will never run.
# Reporting both as "another cycle holds it" makes a broken loop look like a patient one.
loop_lock() {
  local lock="$1"
  mkdir -p "$(dirname "$lock")" 2>/dev/null
  if mkdir "$lock" 2>/dev/null; then
    echo held
    return 0
  fi
  if [ -d "$lock" ]; then
    echo "busy: another cycle holds $lock"
  else
    echo "unusable: cannot create the lock at $lock"
  fi
  return 1
}

# loop_run_checks <policy-file> <log-file> <run-surface-checks:0|1>
# One place decides what green means, so the gate and the agent's own check command cannot drift.
loop_run_checks() {
  local policy="$1" logf="$2" surface="$3" cmd
  cmd=$(loop_cfg "$policy" repo_check)
  if [ -n "$cmd" ]; then
    echo "[check] $cmd" | tee -a "$logf"
    bash -lc "cd '$REPO' && $cmd" >>"$logf" 2>&1 \
      || { echo "[check] FAILED: $cmd" | tee -a "$logf"; return 1; }
  fi
  [ "$surface" != 1 ] && return 0
  while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    echo "[check] $cmd" | tee -a "$logf"
    bash -lc "cd '$REPO' && $cmd" >>"$logf" 2>&1 \
      || { echo "[check] FAILED: $cmd" | tee -a "$logf"; return 1; }
  done < <(loop_cfg_list "$policy" surface_checks)
  return 0
}

# loop_state <records-dir-abs> -> the state summary the tick prompt embeds.
# No associative arrays here: launchd runs these through /bin/bash, which is 3.2 on macOS.
loop_state() {
  local dir="$1" f id state metric prio total s n terminal=0 inconclusive=0 tmp
  tmp=$(mktemp)
  for f in "$dir"/hypotheses/*.yaml; do
    [ -e "$f" ] || continue
    id=$(loop_cfg "$f" id "$(basename "$f" .yaml)")
    state=$(loop_cfg "$f" state unknown)
    metric=$(loop_cfg "$f" metric -)
    prio=$(loop_cfg "$f" priority 9)
    printf '%s\t%s\t%s\t%s\n' "$state" "$id" "$prio" "$metric" >>"$tmp"
  done
  total=$(wc -l <"$tmp" | tr -d ' ')
  echo "records: $total"
  for s in drafted building shipped measuring validated invalidated inconclusive abandoned; do
    n=$(awk -F'\t' -v s="$s" '$1 == s' "$tmp" | wc -l | tr -d ' ')
    echo "  $s: $n"
    case "$s" in
      validated|invalidated|abandoned) terminal=$((terminal + n)) ;;
      inconclusive) terminal=$((terminal + n)); inconclusive=$n ;;
    esac
  done
  if [ "$terminal" -gt 0 ]; then
    echo "theatre check: $inconclusive of $terminal terminal records are inconclusive ($(( inconclusive * 100 / terminal ))%)"
  else
    echo "theatre check: no terminal records yet"
  fi
  [ -f "$dir/paused.flag" ] && echo "PAUSED: $dir/paused.flag exists"
  [ "$total" -gt 0 ] && awk -F'\t' '{printf "%s  %s  priority=%s  metric=%s\n", $2, $1, $3, $4}' "$tmp"
  rm -f "$tmp"
  return 0
}

# --- 稼働状況（D1）-----------------------------------------------------------------
# What goes here is telemetry, never a record: which host ran which tick, and what it cost.
# Verdicts, metrics and hypotheses stay in git, where the integrity tests can reach them. A second
# place that holds judgements is how a repo ends up with two answers and no way to pick one.
#
# The runner writes it, never the agent. The tick has no secret, no curl and no wrangler, and that
# is the property that keeps it from writing its own scorecard. Telemetry is the runner's job
# because the runner already runs as the user.

loop_sql_str() {  # loop_sql_str <value> -> a quoted SQL literal, or NULL when empty
  [ -z "${1:-}" ] && { echo NULL; return; }
  printf "'%s'" "$(printf '%s' "$1" | sed "s/'/''/g")"
}

loop_sql_num() {  # loop_sql_num <value> -> the number, or NULL when it is not one
  case "${1:-}" in
    ''|*[!0-9.-]*) echo NULL ;;
    *) printf '%s' "$1" ;;
  esac
}

loop_status_exec() {  # loop_status_exec <sql>  -> runs it against status_db; silent no-op without one
  # A telemetry write must never decide whether a tick succeeded, so every failure is swallowed.
  local db cwd
  db=$(loop_cfg "$LOOP_YAML" status_db)
  [ -z "$db" ] && return 0
  cwd=$(loop_cfg "$LOOP_YAML" status_db_cwd .)
  ( cd "$REPO/$cwd" 2>/dev/null && npx wrangler d1 execute "$db" --remote --json --command "$1" )     >/dev/null 2>&1 || true
}

loop_status_query() {  # loop_status_query <sql> -> rows as JSON, empty when unavailable
  local db cwd out
  db=$(loop_cfg "$LOOP_YAML" status_db)
  [ -z "$db" ] && return 0
  cwd=$(loop_cfg "$LOOP_YAML" status_db_cwd .)
  out=$( cd "$REPO/$cwd" 2>/dev/null && npx wrangler d1 execute "$db" --remote --json --command "$1" 2>/dev/null ) || return 0
  echo "$out" | jq -c '.[0].results' 2>/dev/null
}

loop_tick_open() {  # loop_tick_open <stamp> <host> <branch>
  loop_status_exec "INSERT OR REPLACE INTO loop_ticks
    (loop_id, stamp, host, started_at, branch, moved)
    VALUES ($(loop_sql_str "$LOOP_ID"), $(loop_sql_str "$1"), $(loop_sql_str "$2"),
            $(loop_sql_str "$(date -u +%FT%TZ)"), $(loop_sql_str "$3"), 0)"
}

loop_tick_close() {  # loop_tick_close <stamp> <host> <rc> <cost> <moved> <pr> <action>
  loop_status_exec "UPDATE loop_ticks SET
      ended_at = $(loop_sql_str "$(date -u +%FT%TZ)"),
      rc       = $(loop_sql_num "$3"),
      cost_usd = $(loop_sql_num "$4"),
      moved    = $(loop_sql_num "$5"),
      pr       = $(loop_sql_num "$6"),
      action   = $(loop_sql_str "$7")
    WHERE loop_id = $(loop_sql_str "$LOOP_ID")
      AND stamp   = $(loop_sql_str "$1")
      AND host    = $(loop_sql_str "$2")"
}

loop_status_summary() {  # prints liveness lines, or nothing when status_db is unset
  local rows hosts
  rows=$(loop_status_query "SELECT host, stamp, ended_at, rc, cost_usd, action, moved
    FROM loop_ticks WHERE loop_id = $(loop_sql_str "$LOOP_ID")
    ORDER BY stamp DESC LIMIT 1")
  [ -z "$rows" ] && return 0
  echo "$rows" | jq -r '.[] | "last tick: \(.stamp) on \(.host)  rc=\(.rc // "?")  $\(.cost_usd // 0)  \(if .moved == 1 then (.action // "moved a record") else "moved nothing" end)"'
  # Two hosts in a day means two loops merge into the same main without seeing each other. The
  # filesystem lock cannot see across machines, so this is the only place it shows up.
  hosts=$(loop_status_query "SELECT host, COUNT(*) n, ROUND(SUM(cost_usd), 2) spent
    FROM loop_ticks WHERE loop_id = $(loop_sql_str "$LOOP_ID")
      AND started_at >= strftime('%Y-%m-%dT%H:%M:%SZ', 'now', '-24 hours')
    GROUP BY host ORDER BY n DESC")
  [ -z "$hosts" ] && return 0
  echo "$hosts" | jq -r '.[] | "  24h: \(.host)  \(.n) ticks  $\(.spent // 0)"'
  [ "$(echo "$hosts" | jq 'length')" -gt 1 ] \
    && echo "  WARNING: more than one host ran this loop in the last 24h"
  return 0
}
