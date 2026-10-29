#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" enable-q8 -- /usr/bin/python3 "$ROOT/server/apply_q8.py" "$@"
