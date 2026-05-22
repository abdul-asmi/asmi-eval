# ─── iMessage Send & Receive ──────────────────────────────────────────────────
# Sending  : AppleScript via osascript
# Receiving : SQLite read from ~/Library/Messages/chat.db
#
# IMPORTANT: Terminal (or whichever app runs this) needs Full Disk Access.
#   System Settings → Privacy & Security → Full Disk Access → enable Terminal

import sqlite3
import subprocess
import time
import os
from datetime import datetime, timezone, timedelta

from config import (
    CHAT_DB,
    ASMI_HANDLE as _CFG_ASMI_HANDLE,
    POLL_INTERVAL,
    SILENCE_AFTER,
    IMESSAGE_SEND_ATTEMPTS,
    IMESSAGE_SEND_RETRY_DELAY,
    IMESSAGE_SEND_VERIFY_TIMEOUT,
    IMESSAGE_SEND_VERIFY_POLL,
)

# Mac Absolute Time epoch (2001-01-01 UTC)
_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _mac_ts(dt: datetime) -> int:
    """Convert a UTC datetime to Mac Absolute Time in nanoseconds."""
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return int((dt - _MAC_EPOCH).total_seconds() * 1_000_000_000)


def _from_mac_ts(ns: int) -> datetime:
    """Convert Mac Absolute Time (nanoseconds) to UTC datetime."""
    return _MAC_EPOCH + timedelta(seconds=ns / 1_000_000_000)


# ─── Send ─────────────────────────────────────────────────────────────────────

def _resolve_handle(handle: str | None) -> str:
    env_handle = os.environ.get("ASMI_HANDLE", "").strip()
    return (handle or env_handle or _CFG_ASMI_HANDLE).strip()


def _normalize_text(text: str) -> str:
    return " ".join((text or "").strip().split())


def _query_recent_outgoing_any_handle(since_mac_ns: int, limit: int = 200) -> list[dict]:
    """
    Read recent outgoing messages regardless of handle/chat mapping.
    Useful when chat_identifier formatting differs from configured ASMI_HANDLE.
    """
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            """
            SELECT m.text, m.date, m.is_from_me
            FROM   message m
            WHERE  m.is_from_me = 1
              AND  m.date       > ?
              AND  m.text       IS NOT NULL
              AND  m.text       != ''
            ORDER BY m.date DESC
            LIMIT ?
            """,
            (since_mac_ns, limit),
        )
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "text": row["text"],
                "timestamp": _from_mac_ts(row["date"]),
                "is_from_me": bool(row["is_from_me"]),
            }
            for row in rows
        ]
    except Exception:
        return []


def _text_matches(candidate: str, needle: str) -> bool:
    c = _normalize_text(candidate or "")
    n = _normalize_text(needle or "")
    if not c or not n:
        return False
    if c == n:
        return True
    if len(n) >= 24 and (n in c or c in n):
        return True
    return False


def _has_outgoing_match(
    handle: str,
    message: str,
    sent_after: datetime,
) -> bool:
    """
    Check chat.db for a just-sent outgoing message matching `message`.
    We require is_from_me and a timestamp at/after the attempted send.
    """
    lookback = sent_after - timedelta(seconds=1)
    msgs = _query_messages(handle, _mac_ts(lookback), limit=200)
    needle = _normalize_text(message)
    for m in msgs:
        if not m.get("is_from_me"):
            continue
        ts = m.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        if ts < lookback:
            continue
        text = m.get("text") or ""
        if _text_matches(text, needle):
            return True

    # Fallback: handle/chat_identifier formatting can vary (+1, country code, email),
    # so also check any recent outgoing message text regardless of handle mapping.
    for m in _query_recent_outgoing_any_handle(_mac_ts(lookback), limit=300):
        ts = m.get("timestamp")
        if not isinstance(ts, datetime):
            continue
        if ts < lookback:
            continue
        if _text_matches(m.get("text") or "", needle):
            return True
    return False


def _wait_for_outgoing_match(
    handle: str,
    message: str,
    sent_after: datetime,
    timeout: float,
    poll_interval: float,
) -> bool:
    deadline = time.time() + max(0.5, float(timeout))
    while time.time() < deadline:
        if _has_outgoing_match(handle, message, sent_after):
            return True
        time.sleep(max(0.2, float(poll_interval)))
    return False


def send_imessage(message: str, handle: str | None = None) -> bool:
    """
    Send an iMessage using AppleScript with retries and chat.db verification.
    Returns True only when a send attempt is confirmed in chat.db.
    """
    stop_file = os.environ.get("ASMI_STOP_FILE", "").strip()
    if stop_file and os.path.exists(stop_file):
        print("  ⏹ Stop requested — not sending another iMessage")
        return False
    handle = _resolve_handle(handle)
    safe_msg = message.replace("\\", "\\\\").replace('"', '\\"')
    attempts = max(1, int(IMESSAGE_SEND_ATTEMPTS or 1))
    retry_delay = max(0.5, float(IMESSAGE_SEND_RETRY_DELAY or 2.0))
    verify_timeout = max(1.0, float(IMESSAGE_SEND_VERIFY_TIMEOUT or 12.0))
    verify_poll = max(0.2, float(IMESSAGE_SEND_VERIFY_POLL or 0.6))
    for attempt in range(1, attempts + 1):
        attempt_started = datetime.now(timezone.utc)
        script = f'''
            tell application "Messages"
                set targetService to 1st service whose service type = iMessage
                set targetBuddy to buddy "{handle}" of targetService
                send "{safe_msg}" to targetBuddy
            end tell
        '''
        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except subprocess.TimeoutExpired:
            print(f"  [send error] attempt {attempt}/{attempts}: AppleScript timed out")
            result = None

        if result is not None and result.returncode == 0:
            if _wait_for_outgoing_match(handle, message, attempt_started, verify_timeout, verify_poll):
                if attempt > 1:
                    print(f"  ✓ Send recovered on attempt {attempt}/{attempts}")
                return True
            # Important: do NOT re-send after a successful AppleScript send.
            # chat.db sync can lag and cause false-negative verification, which
            # would duplicate messages if we retried the send.
            print(
                "  [send verify warning] AppleScript send succeeded but chat.db "
                "did not confirm in time; treating as sent to avoid duplicates"
            )
            return True
        else:
            stderr = (result.stderr or "").strip() if result is not None else ""
            stdout = (result.stdout or "").strip() if result is not None else ""
            details = stderr or stdout or "unknown AppleScript error"
            print(f"  [send error] attempt {attempt}/{attempts}: {details}")

        if attempt < attempts:
            backoff = retry_delay * attempt
            print(f"  ↻ Retrying send in {backoff:.1f}s…")
            time.sleep(backoff)

    print("  [send failed] all attempts exhausted")
    return False


# ─── Receive ──────────────────────────────────────────────────────────────────

def _query_messages(handle: str, since_mac_ns: int, limit: int = 100) -> list[dict]:
    """
    Read messages from chat.db that:
      - came FROM or were SENT TO the given handle
      - arrived after since_mac_ns
    Returns list of dicts: {text, timestamp, is_from_me}
    """
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        
        # Query messages associated with the handle either as the direct sender/recipient handle_id (incoming),
        # or via the chat join table (both incoming and outgoing).
        cur.execute("""
            SELECT DISTINCT m.text, m.date, m.is_from_me
            FROM   message m
            LEFT JOIN handle h ON m.handle_id = h.ROWID
            LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
            LEFT JOIN chat c ON cmj.chat_id = c.ROWID
            WHERE  (h.id = ? OR c.chat_identifier = ?)
              AND  m.date        > ?
              AND  m.text        IS NOT NULL
              AND  m.text        != ''
            ORDER  BY m.date ASC
            LIMIT  ?
        """, (handle, handle, since_mac_ns, limit))
        rows = cur.fetchall()
        conn.close()
        return [
            {
                "text":        row["text"],
                "timestamp":   _from_mac_ts(row["date"]),
                "is_from_me":  bool(row["is_from_me"]),
            }
            for row in rows
        ]
    except Exception as e:
        print(f"  [chat.db error] {e}")
        return []


def catch_up_manual_messages(
    session_start: datetime,
    seen_keys: set,
    handle: str | None = None,
    extra_wait: float = 3.0,
) -> list[dict]:
    """
    After wait_for_responses exits, sleep briefly then do one final chat.db
    query to catch any outgoing (is_from_me) user messages that were slow
    to sync to SQLite — e.g. messages typed manually during the test.
    Only returns messages NOT already in seen_keys, and only is_from_me ones
    that haven't been recorded yet (incoming ones would already be in seen_keys).
    """
    time.sleep(extra_wait)
    handle = _resolve_handle(handle)
    since_ns = _mac_ts(session_start)
    msgs = _query_messages(handle, since_ns, limit=200)
    late_arrivals = []
    for m in msgs:
        key = (m["timestamp"].isoformat(), m["text"], m["is_from_me"])
        if key not in seen_keys:
            seen_keys.add(key)
            if m.get("is_from_me"):
                # Only surface late manual user messages — assistant messages
                # that arrived this late are anomalies we can ignore safely.
                late_arrivals.append(m)
                print(f"  [catch-up] captured late manual message: {m['text'][:80]}")
    return late_arrivals


def wait_for_responses(
    sent_at: datetime,
    count: int = 1,
    timeout: int = 150,
    handle: str | None = None,
    max_responses: int = 10,
    drain_all: bool = False,
    return_raw: bool = False,
    silence_after: float = SILENCE_AFTER,
    seen_keys: set | None = None,
) -> list[str] | list[dict]:
    """
    Wait up to `timeout` seconds for `count` responses from Asmi after `sent_at`.
    Collect replies that arrive after the user message, as well as capturing any manual user messages sent in between.
    """
    handle = _resolve_handle(handle)
    since_ns      = _mac_ts(sent_at)
    deadline      = time.time() + timeout
    collected     = []
    last_new_time = None
    if seen_keys is None:
        seen_keys = set()
    stop_file     = os.environ.get("ASMI_STOP_FILE", "").strip()

    print(f"  Waiting for {count} response(s) (timeout={timeout}s)…", end="", flush=True)
    while time.time() < deadline:
        if stop_file and os.path.exists(stop_file):
            print("\n  ⏹ Stop requested — using responses captured so far")
            break
        msgs = _query_messages(handle, since_ns, limit=max(100, max_responses))
        new_msgs  = []
        for m in msgs:
            key = (m["timestamp"].isoformat(), m["text"], m["is_from_me"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_msgs.append(m)
        if new_msgs:
            for m in new_msgs:
                collected.append(m)
                if not m.get("is_from_me"):
                    assistant_count = sum(1 for x in collected if not x.get("is_from_me"))
                    print(f"\n  ✓ Got response [{assistant_count}/{count}]: {m['text'][:80]}…")
                else:
                    print(f"\n  ✉ Captured user message: {m['text'][:80]}…")
            last_new_time = time.time()
            
        assistant_count = sum(1 for x in collected if not x.get("is_from_me"))
        if assistant_count >= max_responses:
            break
        # If we have received at least one response, ensure we always wait
        # `silence_after` seconds after the last response before ending capture,
        # even if the original timeout would have ended earlier.
        if last_new_time is not None:
            deadline = max(deadline, last_new_time + silence_after)
        if last_new_time is not None and time.time() - last_new_time >= silence_after:
            if drain_all or assistant_count >= count:
                break
        print(".", end="", flush=True)
        time.sleep(POLL_INTERVAL)

    assistant_count = sum(1 for x in collected if not x.get("is_from_me"))
    if assistant_count < count:
        print(f"\n  ⚠ Timeout — got {assistant_count}/{count} responses")
    else:
        print()

    if return_raw:
        return collected
    return [m["text"] for m in collected if not m.get("is_from_me")][:max_responses]
