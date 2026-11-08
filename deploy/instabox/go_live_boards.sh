#!/usr/bin/env bash
set -euo pipefail

export JOB_AGENT_HOME="/data/job-agent"
export JOB_AGENT_CONFIG="/data/job-agent/config"
export INSTABOX_HEADLESS=1
export PATH="/data/workspace/job-agent/.venv/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

if [ -f "$HOME/.config/job-agent/env" ]; then
    set -a; . "$HOME/.config/job-agent/env"; set +a
fi

SETTINGS_FILE="${JOB_AGENT_CONFIG:-/data/job-agent/config}/settings.yaml"
if [ -f "$SETTINGS_FILE" ]; then
    MODE=$(grep -E '^\s*mode:' "$SETTINGS_FILE" | head -n1 | awk '{print $2}' || true)
    if [ "$MODE" != "live" ]; then
        echo "ERROR: apply.mode is not live in settings.yaml (found: '$MODE')" >&2
        exit 1
    fi
fi

echo "=== Enabling Live Submissions on Job Boards ==="

for board in linkedin naukri indeed; do
    echo "--- Enabling $board ---"
    agent apply go-live "$board" --yes || true
done

echo ""
agent apply status

