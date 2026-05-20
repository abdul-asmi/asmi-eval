#!/bin/bash
# Setup Asmi UI as a macOS LaunchAgent so localhost stays up permanently.

set -euo pipefail

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/com.asmi.eval.ui.plist"
PYTHON="/Library/Frameworks/Python.framework/Versions/3.13/bin/python3"

echo "Setting up Asmi UI service..."
echo "  Eval dir: $EVAL_DIR"
echo "  Python:   $PYTHON"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.asmi.eval.ui</string>

  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$EVAL_DIR/ui.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>$EVAL_DIR</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PYTHONUNBUFFERED</key>
    <string>1</string>
    <key>PORT</key>
    <string>8765</string>
    <key>PATH</key>
    <string>/Library/Frameworks/Python.framework/Versions/3.13/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>StandardOutPath</key>
  <string>/tmp/asmi-ui.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/asmi-ui.log</string>

  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>ThrottleInterval</key>
  <integer>5</integer>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/com.asmi.eval.ui" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/com.asmi.eval.ui"
launchctl kickstart -k "gui/$(id -u)/com.asmi.eval.ui"

echo ""
echo "UI service installed."
echo "  URL:       http://127.0.0.1:8765"
echo "  Logs:      tail -f /tmp/asmi-ui.log"
echo "  Stop:      launchctl bootout gui/$(id -u)/com.asmi.eval.ui"
echo "  Start:     launchctl bootstrap gui/$(id -u) $PLIST"
echo "  Restart:   launchctl kickstart -k gui/$(id -u)/com.asmi.eval.ui"
