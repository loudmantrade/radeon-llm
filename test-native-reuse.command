#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" native-reuse -- /usr/bin/python3 "$ROOT/server/native_reuse_experiment.py" "$@"
