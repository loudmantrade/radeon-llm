#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export RADEON_MODEL="$PWD/models/Qwen3-4B-Instruct-2507-Q4_K_M.gguf"
export RADEON_MAX_TOKENS="${RADEON_MAX_TOKENS:-320}"
export RADEON_VULKAN_LOG=logs/qwen3-vulkan-run.log
exec ./run-vulkan.command "${*:-Объясни в пяти предложениях, как работает видеопамять видеокарты.}"
