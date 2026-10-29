#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
./check-vulkan-ops.command
export RADEON_SYSTEM_PROMPT="You are a helpful assistant. Answer briefly in English."
./run-vulkan-safe.command "What is 2 + 2? Answer with one digit."
