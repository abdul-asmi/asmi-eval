#!/bin/bash
set -euo pipefail

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"
LOG="$EVAL_DIR/daemon.log"

cd "$EVAL_DIR"

echo "Asmi Eval daemon restart"
echo "Repo: $EVAL_DIR"

if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  if ! git diff --quiet || ! git diff --cached --quiet || [ -n "$(git ls-files --others --exclude-standard)" ]; then
    STASH_NAME="auto-stash before daemon restart $(date '+%Y-%m-%d %H:%M:%S')"
    echo "Saving local generated changes so git pull cannot get blocked..."
    git stash push -u -m "$STASH_NAME" >/dev/null || true
  fi

  echo "Pulling latest main..."
  if ! git pull --rebase; then
    echo ""
    echo "git pull failed. Your local files were auto-stashed if needed."
    echo "Check with: git status"
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
