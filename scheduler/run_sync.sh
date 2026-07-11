#!/bin/bash
# Wrapper invoked by launchd. Activates the venv (if present) and runs the sync.
set -euo pipefail

PROJECT_DIR="/Users/alexr/cal-task-sync"
cd "$PROJECT_DIR"

if [ -d "$PROJECT_DIR/.venv" ]; then
    # shellcheck disable=SC1091
    source "$PROJECT_DIR/.venv/bin/activate"
fi

exec python -m src.sync
