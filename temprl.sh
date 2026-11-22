#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ "${1:-}" == "--background" ]]; then
    shift
    nohup python3 "$SCRIPT_DIR/app.py" "$@" >/dev/null 2>&1 &
    exit 0
fi

exec python3 "$SCRIPT_DIR/app.py" "$@"
