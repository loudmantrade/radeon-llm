#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" indexed-check -- /usr/bin/python3 "$ROOT/server/check_indexed_cache.py" "$@"
