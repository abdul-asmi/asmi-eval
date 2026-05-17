# ─── Asmi Eval Config ─────────────────────────────────────────────────────────
# Keep this file private — it contains your API key.

ASMI_HANDLE = "+14082307921"          # Asmi's iMessage number

GEMINI_API_KEY = "REDACTED_GEMINI_API_KEY"
GEMINI_MODEL   = "models/gemini-3.1-flash-lite-preview"

RESPONSE_TIMEOUT   = 60    # default seconds to wait for a single response
BURST_WAIT         = 60    # default seconds to wait when expecting multiple responses
POLL_INTERVAL      = 3     # seconds between chat.db polls
BURST_SEND_DELAY   = 1.0   # seconds between rapid-fire sends (default)
SEQUENCE_DELAY     = 12.0  # seconds between sequential task sends
SILENCE_AFTER      = 60.0  # seconds of silence after last response before stopping capture

import os
CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# ─── Command Daemon ────────────────────────────────────────────────────────────
# iMessage yourself to send commands. Set this to your own Apple ID or phone number.
COMMAND_HANDLE  = "+14125922094"   # your Apple ID — send commands here, replies come back here
COMMAND_PREFIX  = "!"                            # commands must start with this (e.g. !run all)
DAEMON_POLL     = 5                              # seconds between inbox checks
EVAL_DIR        = os.path.dirname(os.path.abspath(__file__))  # auto-detects eval folder
REPORTS_DIR     = os.path.join(EVAL_DIR, "reports")           # all results_*.json and report_*.html go here
os.makedirs(REPORTS_DIR, exist_ok=True)


# Set RAILWAY_URL to empty string to disable remote sync.
# Defaulting to the deployed Railway app keeps run results flowing back there
# after each completed run, so the hosted UI stays current.
RAILWAY_URL = os.environ.get("RAILWAY_URL", "https://web-production-a1a67.up.railway.app")
LOCAL_UI_URL = os.environ.get("LOCAL_UI_URL", "http://127.0.0.1:8765")

# ─── Run-All Priority Order ────────────────────────────────────────────────────
# Categories are run in this order when no --category / --id filter is given.
# "onboarding" gets cmd_onboard sent automatically before it starts.
CMD_ONBOARD = "cmd_onboard"   # iMessage that resets Asmi to fresh pre-onboarding state

CATEGORY_RUN_ORDER = [
    "onboarding",           # 1. Pre-onboarding + onboarding (needs fresh state → cmd_onboard sent first)
    "capability",           # 2. What can Asmi do?
    "sticky_message",       # 3. Core: does Asmi respond unprompted?
    "call_dedup",           # 4. Core: no double calls
    "call_summary",         # 5. P0: accuracy, no hallucination
    "voicemail",            # 6. Part of call accuracy
    "task_specific_call",   # 7. Post-onboarding task calls
    "threep_nudge",         # 8. 3P framing
    "location_memory",      # 9. Context retention
    "language_pref",        # 10. Context retention
    "checklist",            # 11. Task management
    "chat_brevity",         # 12. UX
    "chat_flow",            # 13. UX
    "interactive",          # 14. Multi-turn interactive conversations
    "personalization",      # 15. Personalization
    "reengagement",         # 16. Retention (slowest)
    "guardrails",           # 17. Safety
    "timezone",             # 18. Misc
]
