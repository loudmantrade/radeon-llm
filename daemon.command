#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
if [[ $# -eq 0 ]]; then
    set -- status
fi
exec /usr/bin/python3 "$ROOT/server/daemon.py" "$@"
