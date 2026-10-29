#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export GGML_VK_DISABLE_F16=1
export GGML_VK_DISABLE_ASYNC=1
export GGML_VK_SERIALIZE_SUBMISSIONS=1
export GGML_VK_DISABLE_FUSION=1
export GGML_VK_DISABLE_GRAPH_OPTIMIZE=1
export MVK_CONFIG_FAST_MATH_ENABLED=0
export RADEON_VULKAN_LOG=logs/vulkan-safe-run.log
echo "Diagnostic mode: FP16 arithmetic, async execution, fusion, graph optimization and fast math disabled."
exec ./run-vulkan.command "$@"
