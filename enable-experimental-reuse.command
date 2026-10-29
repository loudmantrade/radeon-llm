#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" enable-experimental-reuse -- /usr/bin/python3 "$ROOT/server/apply_experimental_reuse.py" "$@"
