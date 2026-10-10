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
trap 'rmdir "$lock" 2>/dev/null || true; [ -z "${hookdir:-}" ] || rm -rf "$hookdir"' EXIT

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
if ! metrics_script=$(loop_metrics_script "$(loop_cfg "$LOOP_YAML" metrics_backend)" 2>&1); then
  echo "[$(date -u +%FT%TZ)] tick refused: $metrics_script"
  exit 1
fi
mkdir -p "$bin"
for f in loop-config.sh loop-check.sh loop-push.sh loop-merge.sh; do
  cp "$here/$f" "$bin/$f"
done
cp "$here/$metrics_script" "$bin/loop-metrics.sh"
chmod +x "$bin"/loop-*.sh
printf 'LOOP_REPO=%s\nexport LOOP_REPO\n' "$worktree" >"$bin/loop.env"

records="$worktree/$RECORDS_DIR"

# Records in an external store. When loop.yaml sets records_pull / records_push, the records dir is a
# working copy git ignores: the pull fills it before the tick and the push saves it after. Both
# scripts are origin/main's copies, taken before the tick and kept outside the worktree, so the tick
# cannot rewrite the script that saves what it did. A tick whose push cannot run must not start.
policy="$STATE_DIR/loop.yaml.run"
git show "origin/main:${LOOP_YAML#"$REPO"/}" >"$policy" 2>/dev/null || cp "$LOOP_YAML" "$policy"
if ! hooks=$(loop_records_hooks "$policy"); then
  echo "[$(date -u +%FT%TZ)] tick refused: $hooks"
  exit 1
fi
if [ "$hooks" = on ]; then
  if ! hookdir=$(mktemp -d "$STATE_DIR/records-$stamp.XXXXXX"); then
    hookdir=""
    echo "[$(date -u +%FT%TZ)] tick refused: cannot create a scratch dir under $STATE_DIR"
    exit 1
  fi
  mkdir -p "$records"
  for k in records_pull records_push; do
    if ! reason=$(loop_repo_script "$worktree" "$policy" "$k" "$hookdir/$k.sh"); then
      echo "[$(date -u +%FT%TZ)] tick refused: $reason"
      exit 1
    fi
  done
  # Left behind by an earlier tick: the pull would write through it, so a human clears it first.
  odd=$(loop_records_odd "$records")
  if [ -n "$odd" ]; then
    echo "[$(date -u +%FT%TZ)] tick not run: the records dir holds something that is not a plain file: $odd"
    loop_notify "$LOOP_ID" "tick not run: remove what is not a plain file under $RECORDS_DIR ($odd)"
    exit 1
  fi
  pull_log="$LOG_DIR/records-pull-$stamp.log"
  (cd "$worktree" && /bin/bash "$hookdir/records_pull.sh" pull "$worktree" "$records") >"$pull_log" 2>&1
  prc=$?
  if [ "$prc" -ne 0 ]; then
    echo "[$(date -u +%FT%TZ)] tick not run: records_pull exited $prc, see $pull_log"
    loop_notify "$LOOP_ID" "tick not run: records_pull exited $prc ($pull_log)"
    exit 1
  fi
  # A record git can see would ride along in the next commit, and the merge gate would then judge
  # records the store never validated. The repo must ignore everything the pull writes.
  stray=$(git status --porcelain --untracked-files=all -- "$RECORDS_DIR" | head -5 | tr '\n' ' ')
  if [ -n "$stray" ]; then
    echo "[$(date -u +%FT%TZ)] tick not run: records_pull wrote files git does not ignore: $stray"
    loop_notify "$LOOP_ID" "tick not run: gitignore the records the pull writes ($stray)"
    exit 1
  fi
  mkdir -p "$hookdir/pulled" && cp -R "$records/." "$hookdir/pulled/"
fi

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

# The version a baseline names. The tick is denied the commands deploy_verify wraps (wrangler and the
# like can also deploy), so the runner reads it before the tick starts. When it cannot be read the
# prompt says unknown and the tick does not take a baseline.
deploy_now=$(loop_deploy_version "$LOOP_YAML")
prompt_files=("$(dirname "$here")/references/cycle-prompt.md")
[ "$hooks" = on ] && prompt_files+=("$(dirname "$here")/references/cycle-prompt-records-store.md")
prompt=$(sed \
  -e "s|{{REPO}}|$worktree|g" \
  -e "s|{{DEPLOY_VERSION}}|${deploy_now:-unknown}|g" \
  -e "s|{{RECORDS_DIR}}|$RECORDS_DIR|g" \
  -e "s|{{TICK}}|$label-$stamp|g" \
  -e "s|{{SKILL_DIR}}|$(dirname "$here")|g" \
  -e "s|{{STATE_FILE}}|$state_file|g" \
  -e "s|{{HEALTH_FILE}}|$health_file|g" \
  -e "s|{{METRICS}}|$bin/loop-metrics.sh|g" \
  -e "s|{{CHECK}}|$bin/loop-check.sh|g" \
  -e "s|{{PUSH}}|$bin/loop-push.sh|g" \
  -e "s|{{MERGE}}|$bin/loop-merge.sh|g" \
  "${prompt_files[@]}")

budget=$(loop_cfg "$LOOP_YAML" cap_tick_budget_usd 8)
mcp=()
while IFS= read -r t; do [ -n "$t" ] && mcp+=("$t"); done < <(loop_cfg_list "$LOOP_YAML" mcp_tools)

host=$(scutil --get ComputerName 2>/dev/null || hostname -s)
echo "[$(date -u +%FT%TZ)] tick start label=$label branch=$branch budget=\$$budget host=$host"
loop_tick_open "$stamp" "$host" "$branch"
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
    "Bash(echo:*)" \
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

# Saved whatever the agent's rc was: a tick that died after moving a record still moved it. The next
# pull overwrites the working copy, so an unsaved one is copied aside before anyone is told.
# Anything loop_records_odd finds stops the push, and the runner then reads nothing from the working
# copy either, because the digest goes to Slack. cp -R copies a symlink as a symlink, never its target.
unsaved=""
odd=""
if [ "$hooks" = on ]; then
  push_log="$LOG_DIR/records-push-$stamp.log"
  odd=$(loop_records_odd "$records")
  if [ -n "$odd" ]; then
    why="the records dir holds something that is not a plain file: $odd"
    : >"$push_log"
  else
    (cd "$worktree" && /bin/bash "$hookdir/records_push.sh" push "$worktree" "$records" "$hookdir/pulled") >"$push_log" 2>&1
    prc=$?
    why=""
    [ "$prc" -ne 0 ] && why="records_push exited $prc"
  fi
  if [ -n "$why" ]; then
    kept="$STATE_DIR/records-unsaved-$stamp"
    mkdir -p "$kept" && cp -R "$records/." "$kept/"
    echo "[$(date -u +%FT%TZ)] records NOT saved: $why, see $push_log; working copy kept at $kept"
    loop_notify "$LOOP_ID" "records NOT saved: $why ($push_log)"
    unsaved="*記録は保存されていない（records NOT saved）*: ${why}。working copy の写しは $kept"
  fi
fi

# The digest comes from the record, not from the agent's last message. A tick that claims success
# and left nothing on a branch has done nothing.
prs=$(gh pr list --state all --head "$branch" --json number,title,state,url \
  -q '.[] | "• #\(.number) [\(.state)] \(.title)  \(.url)"' 2>/dev/null)
git fetch -q origin 2>/dev/null
ref=origin/main
git rev-parse -q --verify "origin/$branch" >/dev/null && ref="origin/$branch"
journal=""
if [ "$hooks" = on ]; then
  [ -z "$odd" ] && journal=$(loop_journal_added "$hookdir/pulled/journal.md" "$records/journal.md")
else
  journal=$(git diff "$base" "$ref" -- "$RECORDS_DIR/journal.md" 2>/dev/null | sed -n 's/^+\([^+]\)/\1/p')
fi
brief=$(echo "$journal" | awk '
  /^- \*\*(要約|行動|計測|次|要対応)[:：]/ { keep = 1; n = 1; print; next }
  /^- / { keep = 0; next }
  keep && /^[[:space:]]/ { if (++n <= 3) print; next }
  { keep = 0 }' | head -c 2000)
# The digest is what a person reads on their phone: what the tick decided, and whether anything
# needs them. A tick that moved nothing writes no journal, so say that rather than showing a gap.
digest_title="$LOOP_ID · $label · $stamp"
digest="${unsaved:+$unsaved

}${brief:-_この tick は記録を動かさなかった（journal なし）_}

*PRs*
${prs:-• none}
_rc=${rc} · \$${cost}_"
osascript -e "display notification \"rc=$rc cost=\$$cost\" with title \"$LOOP_ID ($label)\"" >/dev/null 2>&1 || true
echo "[$(date -u +%FT%TZ)] $LOOP_ID ($label): rc=$rc cost=\$$cost" >>"$LOG_DIR/notify.log"
# The digest is routine news; the stops and failures above already went out as alerts.
pr_url=$(echo "$prs" | sed -n 's/.*  \(https:[^ ]*\)$/\1/p' | head -1)
# notify_command gets the title as its own argument; the webhook has no title, so it goes on top.
loop_deliver milestone "$digest_title" "$digest" "$pr_url" || loop_slack "*$digest_title*

$digest"
{
  echo "=== $LOOP_ID $label $stamp  rc=$rc cost=\$$cost"
  [ -z "$unsaved" ] || echo "$unsaved"
  echo "${brief:-(tick moved nothing; no journal entry)}"
  echo "PRs: ${prs:-none}"
} | tee -a "$LOG_DIR/digest.log"

# The row is closed from the journal and the PR list, not from the agent's last message. A tick
# that says it shipped and left nothing on the branch closes as moved=0.
moved=0
[ -n "$journal" ] && moved=1
action=$(echo "$journal" | sed -n 's/^- \*\*行動:\*\* *\([A-Z][A-Z]*\).*/\1/p' | head -1)
pr=$(echo "$prs" | sed -n 's/^• #\([0-9]*\) .*/\1/p' | head -1)
loop_tick_close "$stamp" "$host" "$rc" "$cost" "$moved" "$pr" "$action"
exit "$rc"
