#!/bin/bash
# The loop's only push path. Refuses any branch outside branch_prefix so main can change only
# through loop-merge.sh. Never forced.
set -uo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"
[ -f "$here/loop.env" ] && source "$here/loop.env"
loop_env "${LOOP_REPO:?LOOP_REPO not set}" || exit 2
cd "$REPO" || exit 2
branch=$(git symbolic-ref --short HEAD 2>/dev/null) || { echo "detached HEAD" >&2; exit 2; }
case "$branch" in
  "$BRANCH_PREFIX"*) ;;
  *) echo "refusing to push $branch: it does not start with $BRANCH_PREFIX" >&2; exit 2 ;;
esac
git push -u origin "refs/heads/$branch:refs/heads/$branch"
