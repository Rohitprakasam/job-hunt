#!/usr/bin/env bash
set -euo pipefail

export JOB_AGENT_HOME="/data/job-agent"
export JOB_AGENT_CONFIG="/data/job-agent/config"
export INSTABOX_HEADLESS=1
export PATH="/data/workspace/job-agent/.venv/bin:$HOME/.local/bin:/usr/local/bin:$PATH"

if [ -f "$HOME/.config/job-agent/env" ]; then
    set -a; . "$HOME/.config/job-agent/env"; set +a
fi

echo "=== Enabling Live Submissions on Worker ==="

for ats in greenhouse lever ashby; do
    echo "--- Enabling $ats ---"
    agent apply go-live "$ats" --yes
done

echo ""
echo "=== Live Mode Status ==="
echo "Greenhouse, Lever, and Ashby are now enabled for live submissions."
echo "Hourly cycles will submit matching applications automatically."
echo "Stop anytime via WhatsApp: /jobstop or 'agent stop'."
