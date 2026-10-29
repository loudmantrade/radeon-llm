#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export VULKAN_SDK="$PWD/vulkan-sdk/macOS"
export VK_DRIVER_FILES="${RADEON_VULKAN_ICD:-$PWD/moltenvk-1.4.2/MoltenVK/MoltenVK/dynamic/dylib/macOS/MoltenVK_icd.json}"
export VK_ICD_FILENAMES="$VK_DRIVER_FILES"
DRIVER_DIR="$(cd "$(dirname "$VK_DRIVER_FILES")" && pwd)"
export DYLD_LIBRARY_PATH="$DRIVER_DIR:$VULKAN_SDK/lib${DYLD_LIBRARY_PATH:+:$DYLD_LIBRARY_PATH}"
export GGML_VK_DISABLE_F16=1 GGML_VK_DISABLE_ASYNC=1 GGML_VK_SERIALIZE_SUBMISSIONS=1
export GGML_VK_DISABLE_FUSION=1 GGML_VK_DISABLE_GRAPH_OPTIMIZE=1
export MVK_CONFIG_FAST_MATH_ENABLED=0
unset GGML_VK_FORCE_MMVQ GGML_VK_DISABLE_MMVQ
BIN="$PWD/llama.cpp/build-vulkan/bin/llama-completion"
mkdir -p logs
"$BIN" --list-devices > logs/vulkan-diagnostic-devices.log 2>&1
cat logs/vulkan-diagnostic-devices.log
DEVICE=$(awk '/^[[:space:]]*Vulkan[0-9]+:/ && /Radeon/{sub(/:$/, "", $1); print $1; exit}' logs/vulkan-diagnostic-devices.log)
if [[ -z "$DEVICE" ]]; then
    echo "Radeon unavailable. Run this script from the macOS Terminal."
    exit 1
fi
run_case() {
    local label="$1" device="$2" layers="$3"
    echo
    echo "========== $label =========="
    "$BIN" -m models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
        --device "$device" -ngl "$layers" -fa off -c 512 -b 128 -ub 1 -t 8 \
        -ctk f32 -ctv f32 -n 32 --temp 0 --seed 42 --single-turn --no-warmup \
        --verbosity 4 -sys "Отвечай кратко и точно." \
        -p "Вычисли 2 + 2. Ответь только числом." \
        2>&1 | tee "logs/vulkan-diagnostic-$label.log"
}
run_case cpu none 0
run_case gpu-f32-cache "$DEVICE" 99
export GGML_VK_DISABLE_MMVQ=1
run_case gpu-f32-cache-no-mmvq "$DEVICE" 99
echo "Diagnostic runs complete. Logs: logs/vulkan-diagnostic-*.log"
