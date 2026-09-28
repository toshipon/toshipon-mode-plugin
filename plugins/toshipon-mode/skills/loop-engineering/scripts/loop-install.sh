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
echo "The launchd job runs this plugin's scripts from $here. A plugin update changes what runs;"
echo "reinstall is not needed, but read the release notes before updating."
