#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
PROMPT="${*:-Объясни по-русски в трёх предложениях, зачем видеокарте нужна собственная память.}"
./llama.cpp/build/bin/llama-completion -m models/qwen2.5-1.5b-instruct-q4_k_m.gguf \
    --device none -ngl 0 -fa off -c 2048 -b 128 -ub 128 -t 8 -n 160 \
    --temp 0 --seed 42 --single-turn --no-warmup \
    -sys "Ты полезный помощник. Отвечай по-русски кратко и точно." \
    -p "$PROMPT" 2>&1 | tee logs/cpu-run.log
