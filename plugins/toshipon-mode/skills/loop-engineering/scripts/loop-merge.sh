#!/bin/bash
# loop-merge.sh <pr-number>
# The loop's only merge and deploy path.
#
# Exit codes
#   0  merged, and the new version is live
#   1  a check failed, or the merge landed but no new version appeared
#   3  a human must look at it (path outside the allowlist, daily cap, unhealthy system)
#   4  try again later (merge window)
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
pr="${1:?pr number required}"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 1
cd "$REPO" || exit 1
pr_url=$(gh pr view "$pr" --json url -q .url 2>/dev/null)
log() { echo "[$(date -u +%FT%TZ)] merge #$pr: $*" | tee -a "$LOG_DIR/merge.log"; }
refuse() {
  log "REFUSED: $*"
  gh pr comment "$pr" --body "loop-merge refused: $*" >/dev/null 2>&1 || true
  gh pr edit "$pr" --add-label needs-human >/dev/null 2>&1 || true
  loop_notify "$LOOP_ID" "PR #$pr needs a human: $*" alert "$pr_url"
  exit 3
}

# The allowlist comes from origin/main, never from the PR. A PR that widens its own bounds must
# not be judged by the bounds it is proposing.
git fetch -q origin main
policy="$STATE_DIR/loop.yaml.main"
git show "origin/main:${LOOP_YAML#"$REPO"/}" >"$policy" 2>/dev/null \
  || { log "loop.yaml is missing on origin/main"; exit 1; }

allowed=()
while IFS= read -r line; do allowed+=("$line"); done < <(loop_cfg_list "$policy" allowed_paths)
[ "${#allowed[@]}" -eq 0 ] && { log "allowed_paths is empty on origin/main"; exit 1; }

changed=()
while IFS= read -r line; do changed+=("$line"); done < <(gh pr view "$pr" --json files -q '.files[].path' 2>/dev/null)
[ "${#changed[@]}" -eq 0 ] && { log "no changed files reported for #$pr"; exit 1; }

outside=()
for f in "${changed[@]}"; do
  loop_path_allowed "$f" "${allowed[@]}" || outside+=("$f")
done
[ "${#outside[@]}" -gt 0 ] && refuse "outside allowed_paths: ${outside[*]}"

cap_merges=$(loop_cfg "$policy" cap_merges_per_day 3)
today=$(date -u +%Y-%m-%d)
merged_today=$(gh pr list --state merged --limit 60 --json headRefName,mergedAt \
  -q "[.[] | select(.headRefName | startswith(\"$BRANCH_PREFIX\")) | select(.mergedAt | startswith(\"$today\"))] | length" \
  2>/dev/null || echo 0)
[ "${merged_today:-0}" -ge "$cap_merges" ] && refuse "daily merge cap reached ($merged_today/$cap_merges)"

# Pre-flight, not post-merge. A system that is already unhealthy does not get a new deploy on top
# of whatever is wrong, and the next tick's GUARD step is what reads health after a merge.
health_reason=$(loop_health "$REPO" "$(loop_cfg "$policy" deploy_health)") \
  || refuse "pre-flight health: $health_reason"

avoid=$(loop_cfg "$policy" avoid_minutes)
if [ -n "$avoid" ]; then
  lo=${avoid%%-*}; hi=${avoid##*-}; now=$(date +%-M)
  if [ "$now" -ge "$lo" ] && [ "$now" -le "$hi" ]; then
    log "minute $now falls in avoid_minutes ($avoid); try again later"
    exit 4
  fi
fi

head=$(gh pr view "$pr" --json headRefName -q .headRefName)
git checkout -q "$head" && git pull -q --ff-only
if ! git merge --no-edit -q origin/main; then
  git merge --abort
  log "conflict with origin/main"
  exit 1
fi

# Surface checks run when any changed file sits under a surface path. Running all of them, instead
# of matching each surface to its own list, is what keeps the config flat enough to read with sed.
surface_paths=()
while IFS= read -r line; do surface_paths+=("$line"); done < <(loop_cfg_list "$policy" surface_paths)
touched=0
for f in "${changed[@]}"; do
  for s in "${surface_paths[@]}"; do [ "${f#"$s"}" != "$f" ] && touched=1; done
done
loop_run_checks "$policy" "$LOG_DIR/check-$pr.log" "$touched" \
  || { log "checks failed, see $LOG_DIR/check-$pr.log"; exit 1; }

deploy_kind=$(loop_cfg "$policy" deploy_kind auto)
verify=$(loop_cfg "$policy" deploy_verify)
verify_cwd=$(loop_cfg "$policy" deploy_verify_cwd .)
version_of() { loop_deploy_version "$policy"; }
prev=$(version_of)

gh pr merge "$pr" --merge || { log "merge command failed"; exit 1; }
# Not --delete-branch: dropping the local copy makes gh check out the base branch, which fails from
# a worktree and would kill this script after the merge had already landed.
git push -q origin --delete "$head" 2>/dev/null || true
log "merged"
git fetch -q origin main && git checkout -q --detach origin/main

if [ "$deploy_kind" != auto ] || [ -z "$verify" ]; then
  log "no automatic deploy is configured; merge only"
  echo "deploy_version=none"
  loop_notify "$LOOP_ID" "PR #$pr merged (no deploy step)" milestone "$pr_url"
  exit 0
fi

wait_s=$(loop_cfg "$policy" deploy_wait_seconds 300)
cur="$prev"
for _ in $(seq 1 $(( wait_s / 15 ))); do
  sleep 15
  cur=$(version_of)
  [ -n "$cur" ] && [ "$cur" != "$prev" ] && break
done
if [ -n "$cur" ] && [ "$cur" != "$prev" ]; then
  log "deployed: $cur"
  echo "deploy_version=$cur"
  loop_notify "$LOOP_ID" "PR #$pr merged and deployed ($cur)" milestone "$pr_url"
  exit 0
fi

fallback=$(loop_cfg "$policy" deploy_fallback)
if [ -n "$fallback" ] && bash -lc "cd '$REPO/$verify_cwd' && $fallback" >"$LOG_DIR/deploy-$pr.log" 2>&1; then
  cur=$(version_of)
  log "the automatic deploy was silent; the fallback shipped $cur"
  echo "deploy_version=${cur:-fallback}"
  loop_notify "$LOOP_ID" "PR #$pr merged; deployed by the fallback" milestone "$pr_url"
  exit 0
fi
log "merged but no new version appeared in ${wait_s}s and no fallback succeeded"
loop_notify "$LOOP_ID" "PR #$pr merged but NOT deployed. A human must deploy." alert "$pr_url"
exit 1
