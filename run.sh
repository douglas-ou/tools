#!/usr/bin/env bash
set -euo pipefail

PORT=8124
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Check if port is already in use
if lsof -i ":$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $PORT is already in use."
    echo "To stop the existing process: lsof -i :$PORT -t | xargs kill"
    exit 1
fi

echo "Starting Session Viewer on http://localhost:$PORT"
python3 "$SCRIPT_DIR/serve.py"
