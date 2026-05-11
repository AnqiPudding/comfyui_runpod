#!/usr/bin/env bash
set -euo pipefail

export COMFY_HOST="${COMFY_HOST:-127.0.0.1}"
export COMFY_PORT="${COMFY_PORT:-8188}"
export VAST_MODEL_SERVER_HOST="${VAST_MODEL_SERVER_HOST:-127.0.0.1}"
export VAST_MODEL_SERVER_PORT="${VAST_MODEL_SERVER_PORT:-18000}"
export VAST_MODEL_LOG_FILE="${VAST_MODEL_LOG_FILE:-/workspace/logs/vast_comfy_server.log}"

mkdir -p "$(dirname "$VAST_MODEL_LOG_FILE")"

echo "Starting Vast ComfyUI model server on ${VAST_MODEL_SERVER_HOST}:${VAST_MODEL_SERVER_PORT}"
python -m uvicorn vast_comfy_server:app \
  --host "$VAST_MODEL_SERVER_HOST" \
  --port "$VAST_MODEL_SERVER_PORT" \
  > "$VAST_MODEL_LOG_FILE" 2>&1 &

server_pid="$!"

cleanup() {
  kill "$server_pid" 2>/dev/null || true
}
trap cleanup EXIT

echo "Starting Vast PyWorker"
python /workspace/ComfyUI/vast_worker.py
