#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "Use setup.ps1 on Windows. This launcher requires macOS." >&2
    exit 1
fi
PYTHON_BIN=""
for candidate in /opt/homebrew/opt/python@3.11/bin/python3.11 /usr/local/opt/python@3.11/bin/python3.11 /opt/homebrew/bin/python3 /usr/local/bin/python3 "$(command -v python3 || true)"; do
    if [[ -n "$candidate" && -x "$candidate" ]] && "$candidate" -c 'import sys; sys.exit(sys.version_info < (3,11))' 2>/dev/null; then
        PYTHON_BIN="$candidate"
        break
    fi
done
if [[ -z "$PYTHON_BIN" ]]; then
    for arg in "$@"; do
        if [[ "$arg" == "--plan" || "$arg" == "--check" || "$arg" == "--no-deps" ]]; then
            echo "Python 3.11+ is missing. Install it before using this option." >&2
            exit 1
        fi
    done
    if ! command -v brew >/dev/null 2>&1; then
        echo "Install Python 3.11+ from python.org or Homebrew (brew.sh), then rerun setup." >&2
        exit 1
    fi
    brew install python@3.11
    PYTHON_BIN="$(brew --prefix python@3.11)/bin/python3.11"
fi
exec "$PYTHON_BIN" "$ROOT/setup.py" "$@"
