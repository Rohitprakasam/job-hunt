#!/usr/bin/env bash
# Runs or recreates pinned Ollama microservice containers on LLM nodes (rohit-2, rohit-3, rohit-4).
# Pinned version: ollama/ollama:0.40.15

set -euo pipefail

NODE_NAME="${1:-$(hostname)}"
PINNED_IMAGE="ollama/ollama:0.40.15"

# Maximum loaded models: 2 for rohit-2 (embedding + 3b), 1 for rohit-3 / rohit-4 (7b / 8b)
case "$NODE_NAME" in
    *rohit-2) MAX_LOADED=2 ;;
    *)        MAX_LOADED=1 ;;
esac

echo "Deploying Ollama container ($PINNED_IMAGE) on $NODE_NAME (MAX_LOADED=$MAX_LOADED)..."

docker stop ollama 2>/dev/null || true
docker rm ollama 2>/dev/null || true

docker run -d \
    --name ollama \
    --restart unless-stopped \
    -p 127.0.0.1:11434:11434 \
    -v /data/ollama:/root/.ollama \
    -e OLLAMA_NUM_PARALLEL=1 \
    -e OLLAMA_KEEP_ALIVE=30m \
    -e "OLLAMA_MAX_LOADED_MODELS=$MAX_LOADED" \
    -e OLLAMA_HOST=0.0.0.0:11434 \
    "$PINNED_IMAGE"

echo "Ollama container started successfully on $NODE_NAME."

