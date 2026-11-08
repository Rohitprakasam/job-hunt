#!/usr/bin/env bash
set -euo pipefail

JOB_ID="${1:-}"
QUERY="${2:-}"

if [ -z "$JOB_ID" ] || [ -z "$QUERY" ]; then
    echo "Usage: $0 <job_id> <query_text>"
    exit 1
fi

OUT_DIR="/data/hermes/out"
WORK_DIR="/data/hermes/work"
TMP_FILE="${OUT_DIR}/.tmp_${JOB_ID}_$$.md"

mkdir -p "$OUT_DIR" "$WORK_DIR"
cd "$WORK_DIR"

echo "Running research brief generation for Job #${JOB_ID}..."
export PATH="$HOME/.local/bin:/usr/local/bin:$PATH"

# Run hermes non-interactively with timeout and save to temp file
if ! timeout --kill-after=30s 15m hermes chat -q "${QUERY}. Include sections: 1. Overview & Mission 2. Product/Team 3. Tech Stack 4. Recent News 5. 3 Questions to Ask. Must include Sources section with citations." > "$TMP_FILE" 2>&1; then
    echo "Hermes execution failed with non-zero exit code or timeout."
    rm -f "$TMP_FILE"
    exit 1
fi

# Validation:
# 1. Non-empty
if [ ! -s "$TMP_FILE" ]; then
    echo "Error: Brief output file is empty."
    rm -f "$TMP_FILE"
    exit 1
fi

# 2. Contains Sources
if ! grep -qi "Sources" "$TMP_FILE"; then
    echo "Error: Brief output missing Sources section."
    rm -f "$TMP_FILE"
    exit 1
fi

# 3. No server or disconnection error text or raw tool_call dumps
if grep -Ei "HTTP 502|disconnected|sandbox service|temporarily unavailable|RateLimitError|tool_call" "$TMP_FILE"; then
    echo "Error: Brief output contains error or disconnection message."
    rm -f "$TMP_FILE"
    exit 1
fi

# Move valid brief to out/
mv "$TMP_FILE" "${OUT_DIR}/${JOB_ID}.md"
echo "Research brief successfully written to ${OUT_DIR}/${JOB_ID}.md"

