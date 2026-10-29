#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
export VULKAN_SDK="$PWD/vulkan-sdk/macOS"
cmake -S llama.cpp -B llama.cpp/build-vulkan -DCMAKE_BUILD_TYPE=Release \
    -DGGML_VULKAN=ON -DGGML_METAL=OFF -DLLAMA_BUILD_TESTS=OFF \
    -DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_APP=OFF
cmake --build llama.cpp/build-vulkan --config Release -j 6 --target llama-completion llama-bench
