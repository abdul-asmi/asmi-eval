#!/bin/bash
set -euo pipefail

PORT=8765
LOG=/tmp/asmi-ui.log
EVAL_DIR="$(cd "$(dirname "$0")/.." && pwd)"

pkill -f "python.*ui.py" 2>/dev/null || true
sleep 1
nohup /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 "$EVAL_DIR/ui.py" >"$LOG" 2>&1 &
sleep 1

echo "UI restarted."
echo "URL:  http://127.0.0.1:$PORT"
echo "PID:  $(lsof -tiTCP:$PORT -sTCP:LISTEN || echo none)"
