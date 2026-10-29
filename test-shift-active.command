#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
exec "$ROOT/test-native-reuse.command" --phase shift --context 16384 --active-only-shift --probe-decode "$@"
