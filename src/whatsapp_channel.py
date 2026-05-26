"""
WhatsApp channel helpers for the eval system.

Sends outbound WhatsApp messages via the Twilio Messaging API and collects
inbound replies from an in-memory queue populated by the /api/whatsapp/webhook
endpoint in ui.py.

No new SDK required — uses urllib.request just like twilio_phone.py.
"""

from __future__ import annotations

import base64
import os
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Callable

# ---------------------------------------------------------------------------
# Shared inbound queue
# ---------------------------------------------------------------------------
# Each entry: {"text": str, "from_number": str, "received_at": datetime}
# Populated by the Twilio webhook handler in ui.py (imported by reference).
INBOUND_QUEUE: list[dict] = []
_QUEUE_LOCK = threading.Lock()


def enqueue_inbound(text: str, from_number: str) -> None:
    """Called by the webhook endpoint when Asmi replies on WhatsApp."""
    with _QUEUE_LOCK:
        INBOUND_QUEUE.append({
            "text": text.strip(),
            "from_number": from_number,
            "received_at": datetime.now(timezone.utc),
        })


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _sid() -> str:
    return os.environ.get("TWILIO_ACCOUNT_SID", "").strip()


def _token() -> str:
    return os.environ.get("TWILIO_AUTH_TOKEN", "").strip()


def _from_number() -> str:
    return os.environ.get("TWILIO_WHATSAPP_FROM", "").strip()


def whatsapp_configured() -> bool:
    return bool(_sid() and _token() and _from_number())


def _normalize_e164(number: str) -> str:
    raw = (number or "").strip()
    if not raw:
        return ""
    if raw.startswith("+"):
        return raw
    digits = "".join(ch for ch in raw if ch.isdigit())
    return f"+{digits}" if digits else ""


# ---------------------------------------------------------------------------
# Outbound send
# ---------------------------------------------------------------------------

def send_whatsapp(message: str, handle: str) -> bool:
    """
    Send a WhatsApp message FROM the Twilio WhatsApp number TO handle (Asmi).
    Returns True if Twilio accepted the message (2xx), False otherwise.
    """
    sid = _sid()
    token = _token()
    from_num = _from_number()
    to_num = _normalize_e164(handle)

    if not (sid and token and from_num and to_num):
        print(f"  [WhatsApp] Not configured — sid={bool(sid)} token={bool(token)} from={from_num!r} to={to_num!r}")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    data = urllib.parse.urlencode({
        "From": f"whatsapp:{from_num}",
        "To":   f"whatsapp:{to_num}",
        "Body": message,
    }).encode()

    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        if status >= 300:
            print(f"  [WhatsApp] Send failed: HTTP {status} — {body[:200]}")
            return False
        print(f"  [WhatsApp] Sent OK to {to_num}: {message[:60]}")
        return True
    except Exception as e:
        print(f"  [WhatsApp] Send error: {e}")
        return False


def send_whatsapp_template(handle: str, content_sid: str, variables: dict | None = None) -> bool:
    """
    Send an approved WhatsApp template. This is required outside the 24-hour
    customer service window.
    """
    sid = _sid()
    token = _token()
    from_num = _from_number()
    to_num = _normalize_e164(handle)
    template_sid = (content_sid or "").strip()

    if not (sid and token and from_num and to_num and template_sid):
        print(
            "  [WhatsApp] Template not configured — "
            f"sid={bool(sid)} token={bool(token)} from={from_num!r} "
            f"to={to_num!r} content_sid={bool(template_sid)}"
        )
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"
    payload = {
        "From": f"whatsapp:{from_num}",
        "To": f"whatsapp:{to_num}",
        "ContentSid": template_sid,
    }
    if variables:
        import json
        payload["ContentVariables"] = json.dumps(variables)
    data = urllib.parse.urlencode(payload).encode()

    credentials = base64.b64encode(f"{sid}:{token}".encode()).decode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"Basic {credentials}")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status = resp.status
            body = resp.read().decode("utf-8", errors="replace")
        if status >= 300:
            print(f"  [WhatsApp] Template send failed: HTTP {status} — {body[:200]}")
            return False
        print(f"  [WhatsApp] Template sent OK to {to_num}: {template_sid}")
        return True
    except Exception as e:
        print(f"  [WhatsApp] Template send error: {e}")
        return False


def ensure_whatsapp_session(handle: str) -> bool:
    """
    Optionally warm the WhatsApp session with an approved template and wait for
    Asmi to reply, which opens the 24-hour freeform window.
    """
    enabled = os.environ.get("WHATSAPP_WARMUP_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}
    content_sid = os.environ.get("WHATSAPP_WARMUP_TEMPLATE_SID", "").strip()
    if not enabled:
        return True
    if not content_sid:
        print("  [WhatsApp] Warmup enabled but WHATSAPP_WARMUP_TEMPLATE_SID is missing")
        return False

    sent_at = datetime.now(timezone.utc)
    variables_raw = os.environ.get("WHATSAPP_WARMUP_TEMPLATE_VARIABLES", "").strip()
    variables = None
    if variables_raw:
        try:
            import json
            variables = json.loads(variables_raw)
        except Exception as e:
            print(f"  [WhatsApp] Ignoring invalid warmup variables JSON: {e}")

    ok = send_whatsapp_template(handle, content_sid, variables=variables)
    if not ok:
        return False

    timeout = float(os.environ.get("WHATSAPP_WARMUP_TIMEOUT", "60"))
    print("  [WhatsApp] Waiting for warmup reply to open 24-hour window…")
    raw = wait_for_whatsapp_responses(
        sent_at,
        count=1,
        timeout=timeout,
        handle=handle,
        max_responses=3,
        drain_all=False,
        silence_after=3.0,
    )
    if raw:
        print("  [WhatsApp] Warmup reply received; freeform window should be active")
        return True
    print("  [WhatsApp] Warmup timed out; freeform sends may be rejected outside the 24-hour window")
    return False


# ---------------------------------------------------------------------------
# Inbound wait (mirrors imessage.wait_for_responses interface)
# ---------------------------------------------------------------------------

def wait_for_whatsapp_responses(
    sent_at: datetime,
    count: int = 1,
    timeout: float = 30.0,
    handle: str | None = None,
    max_responses: int = 10,
    drain_all: bool = True,
    return_raw: bool = True,
    silence_after: float = 20.0,
    seen_keys: set | None = None,
    is_call_active_fn: Callable[[], bool] | None = None,
) -> list[dict]:
    """
    Poll INBOUND_QUEUE for replies from Asmi's WhatsApp number.
    Returns a list of raw message dicts (compatible with imessage raw format).
    Blocks until count responses are captured, timeout expires, or silence_after
    seconds of quiet pass after the last response.
    """
    if seen_keys is None:
        seen_keys = set()

    asmi_number = _normalize_e164(handle or "")
    deadline = time.time() + max(1.0, float(timeout))
    last_response_at = time.time()
    collected: list[dict] = []

    print(f"  [WhatsApp] Waiting for up to {count} response(s) (timeout={timeout}s, silence={silence_after}s)…")

    while True:
        now = time.time()

        # Hard deadline
        if now >= deadline:
            print(f"  [WhatsApp] Timeout after {timeout}s — got {len(collected)} response(s)")
            break

        # Silence cap
        silence_elapsed = now - last_response_at
        if len(collected) > 0 and silence_elapsed >= silence_after:
            print(f"  [WhatsApp] {silence_after}s silence after last response — stopping")
            break

        # Count goal
        if not drain_all and len(collected) >= count:
            break
        if len(collected) >= max_responses:
            print(f"  [WhatsApp] Reached max_responses={max_responses}")
            break

        # Drain queue
        with _QUEUE_LOCK:
            pending = list(INBOUND_QUEUE)
            INBOUND_QUEUE.clear()

        for entry in pending:
            received_at: datetime = entry["received_at"]
            # Only consider messages received after we sent ours
            if received_at < sent_at:
                continue
            # Filter by sender number if provided
            if asmi_number and not entry["from_number"].endswith(asmi_number.lstrip("+")):
                continue
            key = f"{entry['from_number']}|{entry['text'][:80]}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            raw_entry = {
                "text": entry["text"],
                "timestamp": received_at,
                "is_from_me": False,
                "handle": entry["from_number"],
            }
            collected.append(raw_entry)
            last_response_at = time.time()
            print(f"  [WhatsApp] Got response: {entry['text'][:80]}")
            if not drain_all and len(collected) >= count:
                break

        time.sleep(1.5)

    return collected


def catch_up_whatsapp_messages(sent_at: datetime, seen_keys: set, handle: str | None = None) -> list[dict]:
    """Drain any remaining inbound queue entries (called after wait_for_whatsapp_responses)."""
    asmi_number = _normalize_e164(handle or "")
    results = []
    with _QUEUE_LOCK:
        pending = list(INBOUND_QUEUE)
        INBOUND_QUEUE.clear()
    for entry in pending:
        received_at: datetime = entry["received_at"]
        if received_at < sent_at:
            continue
        if asmi_number and not entry["from_number"].endswith(asmi_number.lstrip("+")):
            continue
        key = f"{entry['from_number']}|{entry['text'][:80]}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        results.append({
            "text": entry["text"],
            "timestamp": received_at,
            "is_from_me": False,
            "handle": entry["from_number"],
        })
    return results
