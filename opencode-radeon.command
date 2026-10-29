#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if command -v opencode >/dev/null 2>&1; then
    OPENCODE_BIN="$(command -v opencode)"
elif [[ -x "$HOME/.opencode/bin/opencode" ]]; then
    OPENCODE_BIN="$HOME/.opencode/bin/opencode"
else
    echo "OpenCode is not on PATH. Install it, then run this launcher from your repository."
    exit 1
fi
export OPENCODE_CONFIG="$ROOT/opencode.json"
if [[ "${1:-}" == "run" ]]; then
    shift
    exec "$OPENCODE_BIN" run --standalone "$@"
fi
exec "$OPENCODE_BIN" --standalone "$@"
