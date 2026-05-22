#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EVAL_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-/Library/Frameworks/Python.framework/Versions/3.13/bin/python3}"
if [ ! -x "$PYTHON" ]; then
  PYTHON="$(command -v python3 || command -v python)"
fi
LOG="$EVAL_DIR/daemon.log"
PLIST="$HOME/Library/LaunchAgents/com.asmi.eval.daemon.plist"
LABEL="com.asmi.eval.daemon"
DOMAIN="gui/$(id -u)"

cd "$EVAL_DIR"

echo "Asmi Eval daemon restart"
echo "Repo: $EVAL_DIR"

if [ -f .env.local ]; then
  set -a
  # shellcheck disable=SC1091
  source .env.local
  set +a
fi

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

if [ -f "$PLIST" ]; then
  echo "Restarting LaunchAgent daemon..."
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
  launchctl bootstrap "$DOMAIN" "$PLIST" 2>/dev/null || launchctl load "$PLIST"
  launchctl enable "$DOMAIN/$LABEL" 2>/dev/null || true
  launchctl kickstart -k "$DOMAIN/$LABEL" 2>/dev/null || true
  sleep 2
  if pgrep -f "$EVAL_DIR/daemon.py" >/dev/null 2>&1; then
    echo "Daemon LaunchAgent is running: $LABEL"
    echo "Log: $LOG"
    echo ""
    echo "Follow logs:"
    echo "  tail -f \"$LOG\""
    exit 0
  fi
  echo "LaunchAgent loaded but daemon process is not alive; falling back to nohup..."
  launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
fi

if command -v osascript >/dev/null 2>&1; then
  echo "Starting daemon from Terminal so Full Disk Access and background lifetime apply..."
  TERMINAL_CMD="cd $(printf '%q' "$EVAL_DIR"); set -a; source .env.local; set +a; pkill -f '[d]aemon.py' 2>/dev/null || true; nohup $(printf '%q' "$PYTHON") -u daemon.py > daemon.log 2>&1 & disown; sleep 2; pgrep -fl '[d]aemon.py'; tail -n 80 daemon.log"
  osascript - "$TERMINAL_CMD" <<'OSA' >/dev/null
on run argv
  tell application "Terminal"
    activate
    do script item 1 of argv
  end tell
end run
OSA
  sleep 3
  if pgrep -f "daemon.py" >/dev/null 2>&1; then
    echo "Daemon started from Terminal."
    echo "Log: $LOG"
    echo ""
    echo "Follow logs:"
    echo "  tail -f \"$LOG\""
    exit 0
  fi
  echo "Terminal start did not produce a live daemon; falling back to local nohup..."
fi

echo "Starting daemon with nohup..."
export PYTHONUNBUFFERED=1
nohup "$PYTHON" -u daemon.py > "$LOG" 2>&1 &
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
