#!/bin/bash
# loop-run.sh <main-repo> [tick-label]
# Runs one headless tick. The agent works in the loop's own worktree, never in a checkout a human
# is using, and reaches main only through loop-merge.sh.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
main_repo="${1:?main repo path required}"
loop_env "$main_repo" || exit 1
label="${2:-tick}"
stamp=$(date -u +%Y%m%d-%H%M)

# The delegation is a human's signature, not a default. An unsigned loop.yaml does not run.
if [ -z "$(loop_cfg "$LOOP_YAML" approved_by)" ] || [ -z "$(loop_cfg "$LOOP_YAML" approved_at)" ]; then
  echo "[$(date -u +%FT%TZ)] tick refused: $LOOP_YAML has no approved_by / approved_at"
  exit 1
fi

# A second loop in the same repo merges to the same main. Sharing one lock costs a delayed tick and
# buys away every merge race.
lock="${LOOP_SHARED_LOCK:-$(loop_cfg "$LOOP_YAML" shared_lock)}"
lock="${lock/#\~/$HOME}"
[ -z "$lock" ] && lock="$STATE_DIR/tick.lock"
if ! reason=$(loop_lock "$lock"); then
  case "$reason" in
    busy:*) echo "[$(date -u +%FT%TZ)] tick skipped: ${reason#busy: }"; exit 0 ;;
    *)      echo "[$(date -u +%FT%TZ)] tick refused: ${reason#unusable: }"; exit 1 ;;
  esac
fi
trap 'rmdir "$lock" 2>/dev/null || true' EXIT

worktree="${LOOP_WORKTREE:-${main_repo%/}-loop}"
if [ ! -d "$worktree" ]; then
  git -C "$main_repo" fetch -q origin main
  git -C "$main_repo" worktree add -q --detach "$worktree" origin/main || exit 1
fi
cd "$worktree" || exit 1
git fetch -q origin main
if [ -n "$(git status --porcelain)" ]; then
  # A previous tick died mid-work. Keep its state on a branch rather than discarding it.
  git checkout -q -b "${BRANCH_PREFIX}abandoned-$stamp" && git add -A \
    && git commit -qm "wip: abandoned loop tick $stamp"
fi
branch="${BRANCH_PREFIX}${label}-${stamp}"
git checkout -q -B "$branch" origin/main
base=$(git rev-parse origin/main)

setup=$(loop_cfg "$LOOP_YAML" setup_command)
[ -n "$setup" ] && bash -lc "cd '$worktree' && $setup" >"$LOG_DIR/setup-$stamp.log" 2>&1

# The gates run from a copy outside the agent's worktree, so an edit the agent makes to a gate can
# never be the gate that judges it.
bin="$BIN_DIR"
if ! reason=$(loop_pathsafe "$bin"); then
  echo "[$(date -u +%FT%TZ)] tick refused: $reason"
  exit 1
fi
mkdir -p "$bin"
for f in loop-config.sh loop-check.sh loop-metrics.sh loop-push.sh loop-merge.sh; do
  cp "$here/$f" "$bin/$f"
done
chmod +x "$bin"/loop-*.sh
printf 'LOOP_REPO=%s\nexport LOOP_REPO\n' "$worktree" >"$bin/loop.env"

records="$worktree/$RECORDS_DIR"
state_file="$STATE_DIR/state-$stamp.txt"
health_file="$STATE_DIR/health-$stamp.txt"
( REPO="$worktree"; loop_state "$records" ) >"$state_file" 2>&1

health=$(loop_cfg "$LOOP_YAML" deploy_health)
if [ -n "$health" ] && [ -x "$worktree/$health" ]; then
  "$worktree/$health" >"$health_file" 2>&1 || true
else
  echo "no health command configured" >"$health_file"
fi

if [ -f "$records/paused.flag" ]; then
  loop_notify "$LOOP_ID" "tick skipped: $RECORDS_DIR/paused.flag is present"
  echo "[$(date -u +%FT%TZ)] tick skipped: paused.flag"
  exit 0
fi

prompt=$(sed \
  -e "s|{{REPO}}|$worktree|g" \
  -e "s|{{RECORDS_DIR}}|$RECORDS_DIR|g" \
  -e "s|{{TICK}}|$label-$stamp|g" \
  -e "s|{{SKILL_DIR}}|$(dirname "$here")|g" \
  -e "s|{{STATE_FILE}}|$state_file|g" \
  -e "s|{{HEALTH_FILE}}|$health_file|g" \
  -e "s|{{METRICS}}|$bin/loop-metrics.sh|g" \
  -e "s|{{CHECK}}|$bin/loop-check.sh|g" \
  -e "s|{{PUSH}}|$bin/loop-push.sh|g" \
  -e "s|{{MERGE}}|$bin/loop-merge.sh|g" \
  "$(dirname "$here")/references/cycle-prompt.md")

budget=$(loop_cfg "$LOOP_YAML" cap_tick_budget_usd 8)
mcp=()
while IFS= read -r t; do [ -n "$t" ] && mcp+=("$t"); done < <(loop_cfg_list "$LOOP_YAML" mcp_tools)

echo "[$(date -u +%FT%TZ)] tick start label=$label branch=$branch budget=\$$budget"
claude -p "$prompt" \
  --model opus \
  --permission-mode acceptEdits \
  --max-budget-usd "$budget" \
  --output-format json \
  --allowedTools "Read" "Edit" "Write" "Glob" "Grep" "Skill" "Agent" "WebSearch" "WebFetch" \
    ${mcp[@]+"${mcp[@]}"} \
    "Bash(git status:*)" "Bash(git diff:*)" "Bash(git log:*)" "Bash(git show:*)" \
    "Bash(git add:*)" "Bash(git commit:*)" "Bash(git checkout:*)" "Bash(git switch:*)" \
    "Bash(git branch:*)" "Bash(git fetch:*)" "Bash(git merge:*)" "Bash(git rev-parse:*)" \
    "Bash(gh pr create:*)" "Bash(gh pr view:*)" "Bash(gh pr list:*)" "Bash(gh pr diff:*)" \
    "Bash($bin/loop-check.sh)" "Bash($bin/loop-metrics.sh:*)" \
    "Bash($bin/loop-push.sh)" "Bash($bin/loop-merge.sh:*)" \
    "Bash(cd:*)" "Bash(ls:*)" "Bash(cat:*)" "Bash(head:*)" "Bash(tail:*)" "Bash(grep:*)" \
    "Bash(sed -n:*)" "Bash(jq:*)" "Bash(wc:*)" "Bash(date:*)" "Bash(mkdir:*)" \
  --disallowedTools "Bash(gh pr merge:*)" "Bash(gh pr edit:*)" "Bash(git push:*)" \
    "Bash(npm run deploy:*)" "Bash(npx wrangler:*)" "Bash(wrangler:*)" \
    "Bash(curl:*)" "Bash(op:*)" "Bash(security:*)" \
    "Bash(git config:*)" "Bash(git remote:*)" "Bash(git -c:*)" "Bash(git clone:*)" \
    "Bash(git filter-branch:*)" "Bash(git reset --hard:*)" "Bash(rm:*)" \
  >"$LOG_DIR/tick-$stamp.json" 2>>"$LOG_DIR/tick-$stamp.err"
rc=$?

cost=$(jq -r 'if (.total_cost_usd | type) == "number" then (.total_cost_usd * 100 | round / 100 | tostring) else "?" end' \
  "$LOG_DIR/tick-$stamp.json" 2>/dev/null)
echo "[$(date -u +%FT%TZ)] tick end label=$label rc=$rc cost=\$$cost"

# The digest comes from the record, not from the agent's last message. A tick that claims success
# and left nothing on a branch has done nothing.
prs=$(gh pr list --state all --head "$branch" --json number,title,state,url \
  -q '.[] | "• #\(.number) [\(.state)] \(.title)  \(.url)"' 2>/dev/null)
git fetch -q origin 2>/dev/null
ref=origin/main
git rev-parse -q --verify "origin/$branch" >/dev/null && ref="origin/$branch"
journal=$(git diff "$base" "$ref" -- "$RECORDS_DIR/journal.md" 2>/dev/null | sed -n 's/^+\([^+]\)/\1/p')
brief=$(echo "$journal" | awk '
  /^- \*\*(要約|行動|計測|次|要対応)[:：]/ { keep = 1; n = 1; print; next }
  /^- / { keep = 0; next }
  keep && /^[[:space:]]/ { if (++n <= 3) print; next }
  { keep = 0 }' | head -c 2000)
# The digest is what a person reads on their phone: what the tick decided, and whether anything
# needs them. A tick that moved nothing writes no journal, so say that rather than showing a gap.
digest="*$LOOP_ID · $label · $stamp*

${brief:-_この tick は記録を動かさなかった（journal なし）_}

*PRs*
${prs:-• none}
_rc=${rc} · \$${cost}_"
osascript -e "display notification \"rc=$rc cost=\$$cost\" with title \"$LOOP_ID ($label)\"" >/dev/null 2>&1 || true
echo "[$(date -u +%FT%TZ)] $LOOP_ID ($label): rc=$rc cost=\$$cost" >>"$LOG_DIR/notify.log"
loop_slack "$digest"
{
  echo "=== $LOOP_ID $label $stamp  rc=$rc cost=\$$cost"
  echo "${brief:-(tick moved nothing; no journal entry)}"
  echo "PRs: ${prs:-none}"
} | tee -a "$LOG_DIR/digest.log"
exit "$rc"
