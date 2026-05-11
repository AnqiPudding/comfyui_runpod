#!/usr/bin/env bash
set -euo pipefail

provider="${SERVERLESS_PROVIDER:-runpod}"
model_dir="${MODEL_VOLUME_DIR:-${VAST_MODEL_DIR:-}}"

if [[ -z "$model_dir" ]]; then
  if [[ -d /runpod-volume ]]; then
    model_dir="/runpod-volume/models"
  else
    model_dir="/data/models"
  fi
fi

mkdir -p "$model_dir"
mkdir -p /runpod-volume

if [[ ! -e /runpod-volume/models ]]; then
  ln -s "$model_dir" /runpod-volume/models
fi

echo "Model volume directory: $model_dir"
echo "Serverless provider: $provider"

case "$provider" in
  vast)
    exec /workspace/start-vast.sh
    ;;
  runpod)
    exec python /workspace/ComfyUI/runpod_handler.py
    ;;
  *)
    echo "Unknown SERVERLESS_PROVIDER=$provider; expected runpod or vast" >&2
    exit 1
    ;;
esac
