#!/bin/bash
set -euo pipefail

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
LOG="$EVAL_DIR/daemon.log"

cd "$EVAL_DIR"

echo "Asmi Eval daemon restart"
echo "Repo: $EVAL_DIR"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Pulling latest main..."
  if ! git pull --rebase; then
    echo ""
    echo "git pull failed because local files changed."
    echo "Run this once, then retry ./restart_daemon.sh:"
    echo "  git stash push -u -m \"local daemon restart stash\""
    exit 1
  fi
fi

echo "Stopping existing daemon processes..."
pkill -f daemon.py 2>/dev/null || true
sleep 1

echo "Starting daemon with nohup..."
nohup "$PYTHON" daemon.py > "$LOG" 2>&1 &
PID=$!

sleep 1
if ps -p "$PID" >/dev/null 2>&1; then
  echo "Daemon started: pid=$PID"
  echo "Log: $LOG"
  echo ""
  echo "Follow logs:"
  echo "  tail -f \"$LOG\""
else
  echo "Daemon exited immediately. Last log lines:"
  tail -80 "$LOG" || true
  exit 1
fi

