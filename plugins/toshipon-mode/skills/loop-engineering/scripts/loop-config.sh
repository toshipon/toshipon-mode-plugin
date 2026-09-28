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
  mkdir -p "$STATE_DIR" "$LOG_DIR"
}

loop_notify() {  # loop_notify <title> <message>
  osascript -e "display notification \"${2//\"/\\\"}\" with title \"${1//\"/\\\"}\"" >/dev/null 2>&1 || true
  echo "[$(date -u +%FT%TZ)] $1: $2" >>"$LOG_DIR/notify.log"
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
