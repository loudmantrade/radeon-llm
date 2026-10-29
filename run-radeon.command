#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
BIN="$PWD/llama.cpp/build/bin/llama-completion"
MODEL="$PWD/models/qwen2.5-1.5b-instruct-q4_k_m.gguf"
mkdir -p logs
"$BIN" --list-devices > logs/devices.log 2>&1
cat logs/devices.log
DEVICE=$(awk '/Radeon/ && /:/{sub(/:$/, "", $1); print $1; exit}' logs/devices.log)
if [[ -z "$DEVICE" ]]; then
    echo "Radeon is not available to Metal. Run this script in the macOS Terminal."
    exit 1
fi
PROMPT="${*:-Объясни по-русски в трёх предложениях, зачем видеокарте нужна собственная память.}"
echo "Using Radeon device: $DEVICE"
"$BIN" -m "$MODEL" --device "$DEVICE" -ngl 99 -fa off -c 2048 -b 128 -ub 128 \
    -n 160 --temp 0 --seed 42 --single-turn --no-warmup --verbosity 4 \
    -sys "Ты полезный помощник. Отвечай по-русски кратко и точно." \
    -p "$PROMPT" 2>&1 | tee logs/radeon-run.log
