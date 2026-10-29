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
mkdir -p logs
echo "Compare small Vulkan matrix operations against the CPU reference."
echo "Driver: $VK_DRIVER_FILES"
DYLD_PRINT_LIBRARIES=1 ./llama.cpp/build-vulkan/bin/llama-completion --list-devices \
    > logs/vulkan-loaded-libraries.log 2>&1
awk '/libMoltenVK.dylib/ { print "Loaded library: " $0 }' logs/vulkan-loaded-libraries.log
./llama.cpp/build-vulkan/bin/test-backend-ops test -b Vulkan0 -o MUL_MAT \
    -p 'type_a=(f32|f16|q4_K|q6_K),type_b=f32,m=16,n=(1|8),k=(256|4096),' \
    2>&1 | tee logs/vulkan-ops.log
