#!/bin/bash
# ─── Setup Asmi Eval Daemon as a macOS LaunchAgent ────────────────────────────
# Run once:  bash setup_daemon.sh
# This makes the daemon start automatically and restart if it crashes.

EVAL_DIR="$(cd "$(dirname "$0")" && pwd)"
WRAPPER="$EVAL_DIR/run_daemon.sh"
PLIST="$HOME/Library/LaunchAgents/com.asmi.eval.daemon.plist"

echo "Setting up Asmi Eval Daemon..."
echo "  Eval dir: $EVAL_DIR"
echo "  Wrapper:  $WRAPPER"

# Write the LaunchAgent plist
cat > "$PLIST" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.asmi.eval.daemon</string>

    <key>ProgramArguments</key>
    <array>
        <string>/bin/bash</string>
        <string>$WRAPPER</string>
    </array>

    <key>WorkingDirectory</key>
    <string>$EVAL_DIR</string>

    <key>StandardOutPath</key>
    <string>$EVAL_DIR/daemon.log</string>

    <key>StandardErrorPath</key>
    <string>$EVAL_DIR/daemon.log</string>

    <key>RunAtLoad</key>
    <true/>

    <key>KeepAlive</key>
    <true/>

    <key>ThrottleInterval</key>
    <integer>10</integer>
</dict>
</plist>
EOF

# Load it
launchctl unload "$PLIST" 2>/dev/null
launchctl load "$PLIST"

echo ""
echo "✅ Daemon installed and running."
echo ""
echo "Commands:"
echo "  View logs:   tail -f $EVAL_DIR/daemon.log"
echo "  Stop:        launchctl unload $PLIST"
echo "  Start:       launchctl load $PLIST"
echo "  Uninstall:   launchctl unload $PLIST && rm $PLIST"
echo ""
echo "Now iMessage yourself at: abdulgaffoor1729@gmail.com"
echo "Send commands from your iPhone — replies come back to the same thread."
echo "Send:  !help"
