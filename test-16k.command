#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec /usr/bin/python3 "$ROOT/server/logged_run.py" context16k -- /usr/bin/python3 "$ROOT/server/context_experiment.py" "$@"
