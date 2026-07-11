#!/bin/bash
# Install/load the launchd agent that runs the sync hourly.
set -euo pipefail

PROJECT_DIR="/Users/alexr/cal-task-sync"
LABEL="com.alexr.caltasksync"
PLIST_SRC="$PROJECT_DIR/scheduler/$LABEL.plist"
PLIST_DST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$PROJECT_DIR/logs"
mkdir -p "$HOME/Library/LaunchAgents"

cp "$PLIST_SRC" "$PLIST_DST"
chmod +x "$PROJECT_DIR/scheduler/run_sync.sh"

# Reload cleanly if already loaded.
launchctl unload "$PLIST_DST" 2>/dev/null || true
launchctl load "$PLIST_DST"

echo "Loaded $LABEL. It will run now and then hourly."
echo "Logs: $PROJECT_DIR/logs/sync.out.log and sync.err.log"
echo "To stop: launchctl unload \"$PLIST_DST\""
