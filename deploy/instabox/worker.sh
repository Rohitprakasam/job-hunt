#!/usr/bin/env bash
set -euo pipefail

echo "=== Deploying Job-Agent Worker on InstaBox ==="

PASS="${SUDO_PASS:-}"
run_sudo() {
    if sudo -n true 2>/dev/null; then
        sudo "$@"
    elif [ -n "$PASS" ]; then
        echo "$PASS" | sudo -S "$@"
    else
        echo "Error: sudo requires password but SUDO_PASS is not set." >&2
        exit 1
    fi
}

# 1. Base directories
run_sudo mkdir -p /data/job-agent/config /data/job-agent/data /data/workspace/job-agent
run_sudo chown -R "$(id -u):$(id -g)" /data/job-agent /data/workspace

# 2. Environment variables in ~/.profile
PROFILE="$HOME/.profile"
touch "$PROFILE"
for line in \
  'export JOB_AGENT_HOME=/data/job-agent' \
  'export JOB_AGENT_CONFIG=/data/job-agent/config' \
  'export INSTABOX_HEADLESS=1' \
  'export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"'; do
    if ! grep -Fxq "$line" "$PROFILE"; then
        echo "$line" >> "$PROFILE"
    fi
done

# Export into current shell
export JOB_AGENT_HOME=/data/job-agent
export JOB_AGENT_CONFIG=/data/job-agent/config
export INSTABOX_HEADLESS=1
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# 3. Apt packages for LaTeX and dependencies
echo "Updating apt and installing LaTeX packages..."
run_sudo apt-get update -qq
run_sudo apt-get install -y --no-install-recommends \
  texlive-xetex \
  texlive-latex-recommended \
  texlive-latex-extra \
  texlive-fonts-recommended \
  fonts-crosextra-caladea \
  chromium \
  git \
  curl \
  build-essential

# 4. Check uv
if ! command -v uv &>/dev/null; then
    echo "Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

# 5. Repo workspace setup
cd /data/workspace/job-agent

if [ -f "pyproject.toml" ]; then
    echo "Syncing uv virtual environment..."
    uv sync --locked
    echo "Installing playwright browser..."
    uv run playwright install chromium || echo "Playwright install warning; using system chromium"
fi

echo "=== Worker deployment complete ==="
