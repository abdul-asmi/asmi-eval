# ─── Asmi Eval Config ─────────────────────────────────────────────────────────
# Keep this file private — it contains your API key.

ASMI_HANDLE = "+14082307921"          # Asmi's iMessage number

GEMINI_API_KEY = "REDACTED_GEMINI_API_KEY"
GEMINI_MODEL   = "models/gemini-3.1-flash-lite-preview"

RESPONSE_TIMEOUT   = 150   # seconds to wait for a single response
BURST_WAIT         = 240   # seconds to wait when expecting multiple responses
POLL_INTERVAL      = 3     # seconds between chat.db polls
BURST_SEND_DELAY   = 1.0   # seconds between rapid-fire sends (default)
SEQUENCE_DELAY     = 12.0  # seconds between sequential task sends

import os
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# ─── Command Daemon ────────────────────────────────────────────────────────────
# iMessage yourself to send commands. Set this to your own Apple ID or phone number.
COMMAND_HANDLE  = "abdulgaffoor1729@gmail.com"   # your Apple ID — send commands here, replies come back here
COMMAND_PREFIX  = "!"                            # commands must start with this (e.g. !run all)
DAEMON_POLL     = 5                              # seconds between inbox checks
EVAL_DIR        = os.path.dirname(os.path.abspath(__file__))  # auto-detects eval folder
