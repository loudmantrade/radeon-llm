#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export VULKAN_SDK="$PWD/vulkan-sdk/macOS"
export VK_DRIVER_FILES="${RADEON_VULKAN_ICD:-$PWD/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json}"
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
DRIVER_DIR="$(cd "$(dirname "$VK_DRIVER_FILES")" && pwd)"
export DYLD_LIBRARY_PATH="$DRIVER_DIR:$VULKAN_SDK/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
BIN="$PWD/llama.cpp/build-vulkan/bin/llama-completion"
mkdir -p logs
if ! "$BIN" --list-devices > logs/vulkan-devices.log 2>&1; then
    cat logs/vulkan-devices.log
    exit 1
fi
cat logs/vulkan-devices.log
DEVICE=$(awk '/^[[:space:]]*Vulkan[0-9]+:/ && /Radeon/{sub(/:$/, "", $1); print $1; exit}' logs/vulkan-devices.log)
if [[ -z "$DEVICE" ]]; then
    echo "Radeon is not available to Vulkan. Run this script in the macOS Terminal."
    exit 1
fi
if [[ "${1:-}" == "--list-devices" ]]; then
    exit 0
fi
PROMPT="${*:-Объясни по-русски в трёх предложениях, зачем видеокарте нужна собственная память.}"
echo "Using Radeon Vulkan device: $DEVICE"
"$BIN" -m "${RADEON_MODEL:-$PWD/models/qwen2.5-1.5b-instruct-q4_k_m.gguf}" \
    --device "$DEVICE" -ngl 99 -fa off -c 2048 -b 128 -ub 128 -t 8 \
    -n "${RADEON_MAX_TOKENS:-160}" --temp 0 --seed 42 --single-turn --no-warmup --verbosity 4 \
    -sys "${RADEON_SYSTEM_PROMPT:-Ты полезный помощник. Отвечай по-русски кратко и точно.}" \
    -p "$PROMPT" 2>&1 | tee "${RADEON_VULKAN_LOG:-logs/vulkan-run.log}"
