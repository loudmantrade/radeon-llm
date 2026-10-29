#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
"$ROOT/daemon.command" update-scripts
exec "$ROOT/check-kv-cache.command"
