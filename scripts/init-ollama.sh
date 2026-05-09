#!/usr/bin/env bash
# Pull the configured Ollama model into the running ollama container.
# The compose entrypoint also pulls on first start, but this script lets you
# preload additional models or refresh after changing OLLAMA_MODEL.
#
# Usage:
#   ./scripts/init-ollama.sh                # pulls $OLLAMA_MODEL or qwen2.5:3b
#   OLLAMA_MODEL=llama3.2:3b ./scripts/init-ollama.sh

set -euo pipefail

MODEL="${OLLAMA_MODEL:-qwen2.5:3b}"

if ! docker compose ps ollama --format '{{.Name}}' | grep -q ollama; then
    echo "ERROR: ollama container is not running. Start it with: docker compose up -d ollama"
    exit 1
fi

echo "Pulling $MODEL into the ollama container..."
docker compose exec ollama ollama pull "$MODEL"
echo "Done."
docker compose exec ollama ollama list
