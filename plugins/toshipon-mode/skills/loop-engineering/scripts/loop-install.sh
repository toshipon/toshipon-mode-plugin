#!/bin/bash
# loop-install.sh <main-repo> [hour] [minute]
# Installs (or reinstalls) the launchd job that runs one tick a day. Uninstall with --uninstall.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
source "$here/loop-config.sh"

if [ "${1:-}" = "--uninstall" ]; then
  label="${2:?loop id required for --uninstall}"
  launchctl bootout "gui/$(id -u)/com.toshipon.loop.$label" 2>/dev/null || true
  rm -f "$HOME/Library/LaunchAgents/com.toshipon.loop.$label.plist"
  echo "uninstalled com.toshipon.loop.$label"
  exit 0
fi

main_repo="${1:?main repo path required}"
loop_env "$main_repo"
hour="${2:-15}"
minute="${3:-30}"

# Preflight. Each of these makes the loop refuse at run time or at merge time, and finding that out
# on the first tick a week later is how a scheduled job becomes a job nobody trusts.
if [ -z "$(loop_cfg "$LOOP_YAML" approved_by)" ] || [ -z "$(loop_cfg "$LOOP_YAML" approved_at)" ]; then
  echo "refusing to install: $LOOP_YAML has no approved_by / approved_at." >&2
  echo "The delegation is a signature, not a default. Sign it first." >&2
  exit 1
fi
if ! reason=$(loop_health "$main_repo" "$(loop_cfg "$LOOP_YAML" deploy_health)"); then
  echo "refusing to install: $reason" >&2
  echo "loop-merge.sh runs the same check before every merge, so the loop could never merge here." >&2
  echo "Either make that command work on this machine, or clear deploy_health in $LOOP_YAML and" >&2
  echo "accept that merges go out without a health gate." >&2
  exit 1
fi
label="com.toshipon.loop.$LOOP_ID"
agents="$HOME/Library/LaunchAgents"
mkdir -p "$agents"
launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true

cat >"$agents/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$here/loop-run.sh</string><string>$main_repo</string><string>daily</string>
  </array>
  <key>StartCalendarInterval</key><dict>
    <key>Hour</key><integer>$hour</integer><key>Minute</key><integer>$minute</integer>
  </dict>
  <key>StandardOutPath</key><string>$LOG_DIR/launchd.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/launchd.log</string>
</dict></plist>
PLIST

launchctl bootstrap "gui/$(id -u)" "$agents/$label.plist"
launchctl list | grep "$LOOP_ID" || true
echo
echo "installed $label at $hour:$minute local time"
echo "logs: $LOG_DIR"
echo
echo "The job runs the scripts at $here, and that path carries the plugin version. A plugin update"
echo "installs a NEW versioned directory and leaves this one in place, so launchd keeps running the"
echo "old scripts and says nothing. Re-run this command after every plugin update."
