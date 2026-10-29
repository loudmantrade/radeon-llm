#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" kv-check -- /usr/bin/python3 "$ROOT/server/check_kv_cache.py" "$@"
