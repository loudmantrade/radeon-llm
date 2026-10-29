#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" warm-opencode -- /usr/bin/python3 "$ROOT/server/warm_opencode.py" "$@"
