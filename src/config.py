# ─── Asmi Eval Config ─────────────────────────────────────────────────────────
# Runtime secrets should come from environment variables, not this file.

import os

ASMI_HANDLE = "+14082307921"          # Asmi's iMessage number

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "models/gemini-3.1-flash-lite-preview").strip()

RESPONSE_TIMEOUT   = 60    # default seconds to wait for a single response
BURST_WAIT         = 60    # default seconds to wait when expecting multiple responses
POLL_INTERVAL      = 3     # seconds between chat.db polls
BURST_SEND_DELAY   = 1.0   # seconds between rapid-fire sends (default)
SEQUENCE_DELAY     = 12.0  # seconds between sequential task sends
SILENCE_AFTER      = 30.0  # seconds of silence after last response before stopping capture
JUDGE_DELAY        = 4     # seconds between Gemini judge calls (free tier ~15 RPM)

# iMessage send hardening (prevents silent send failures from one-shot AppleScript)
IMESSAGE_SEND_ATTEMPTS      = int(os.environ.get("IMESSAGE_SEND_ATTEMPTS", "3"))
IMESSAGE_SEND_RETRY_DELAY   = float(os.environ.get("IMESSAGE_SEND_RETRY_DELAY", "2.0"))
IMESSAGE_SEND_VERIFY_TIMEOUT = float(os.environ.get("IMESSAGE_SEND_VERIFY_TIMEOUT", "12.0"))
IMESSAGE_SEND_VERIFY_POLL   = float(os.environ.get("IMESSAGE_SEND_VERIFY_POLL", "0.6"))

CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

# ─── Command Daemon ────────────────────────────────────────────────────────────
# iMessage yourself to send commands. Set this to your own Apple ID or phone number.
COMMAND_HANDLE  = "+14125922094"   # your Apple ID — send commands here, replies come back here
COMMAND_PREFIX  = "!"                            # commands must start with this (e.g. !run all)
DAEMON_POLL     = 5                              # seconds between inbox checks
EVAL_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # project root (one up from src/)
REPORTS_DIR     = os.path.join(EVAL_DIR, "reports")           # all results_*.json and report_*.html go here
os.makedirs(REPORTS_DIR, exist_ok=True)


# Remote UI sync target (Render recommended). Backwards compatible with RAILWAY_URL.
# Set REMOTE_UI_URL="" (and/or RAILWAY_URL="") to disable remote sync.
REMOTE_UI_URL = os.environ.get("REMOTE_UI_URL", "").strip()
RAILWAY_URL = (REMOTE_UI_URL or os.environ.get("RAILWAY_URL", "https://web-production-a1a67.up.railway.app")).strip()
LOCAL_UI_URL = os.environ.get("LOCAL_UI_URL", "http://127.0.0.1:8765")

# Shared secret for daemon → remote UI API calls. Configure the same value on the server as DAEMON_TOKEN.
DAEMON_TOKEN = os.environ.get("DAEMON_TOKEN", "").strip()

# Supabase owner user id (auth.users.id) for associating runs in the hosted UI.
# Required when using Supabase-backed hosting (Render).
DAEMON_OWNER_USER_ID = os.environ.get("DAEMON_OWNER_USER_ID", "").strip()

# ─── Run-All Priority Order ────────────────────────────────────────────────────
# Categories are run in this order when no --category / --id filter is given.
# "onboarding" gets cmd_onboard sent automatically before it starts.
CMD_ONBOARD = "cmd_onboard"   # iMessage that resets Asmi to fresh pre-onboarding state

CATEGORY_RUN_ORDER = [
    "core test",            # Quick representative sweep: first test from each category
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
    "call_eval",            # 19. End-to-end call evaluation via ElevenLabs persona
]

# ─── ElevenLabs Call Eval ──────────────────────────────────────────────────────
# Used for test type "call_eval": Asmi places a call to CALL_EVAL_PHONE (a Twilio
# number), ElevenLabs answers as a persona, and we fetch the call transcript.
#
# Setup:
#   1. Buy a Twilio voice number (twilio.com, ~$1/month)
#   2. In ElevenLabs dashboard → Conversational AI → create an agent
#      (enable "system_prompt" overrides under agent Security settings)
#   3. ElevenLabs → Phone Numbers → Import → Twilio → assign the agent
#   4. Set the three env vars below in .env.local

ELEVENLABS_API_KEY       = os.environ.get("ELEVENLABS_API_KEY", "").strip()
ELEVENLABS_AGENT_ID      = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()

# The Twilio phone number Asmi will call during call_eval tests.
# Use E.164 format in the env (e.g. +14125551234) but store without + here.
CALL_EVAL_PHONE          = os.environ.get("CALL_EVAL_PHONE", "").strip().lstrip("+")

# How long to wait for the ElevenLabs call to complete before giving up.
CALL_TRANSCRIPT_TIMEOUT  = int(os.environ.get("CALL_TRANSCRIPT_TIMEOUT", "240"))

# Twilio hard call cap (seconds) for call_eval.
# This enforces connected call duration by ending the active Twilio call via REST.
CALL_EVAL_MAX_DURATION = int(os.environ.get("CALL_EVAL_MAX_DURATION", "100"))
