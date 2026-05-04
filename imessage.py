# ─── iMessage Send & Receive ──────────────────────────────────────────────────
# Sending  : AppleScript via osascript
# Receiving : SQLite read from ~/Library/Messages/chat.db
#
# IMPORTANT: Terminal (or whichever app runs this) needs Full Disk Access.
#   System Settings → Privacy & Security → Full Disk Access → enable Terminal

import sqlite3
import subprocess
import time
from datetime import datetime, timezone, timedelta

from config import CHAT_DB, ASMI_HANDLE, POLL_INTERVAL

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

def send_imessage(message: str, handle: str = ASMI_HANDLE) -> bool:
    """Send an iMessage using AppleScript. Returns True on success."""
    # Escape double quotes in message body
    safe_msg = message.replace('"', '\\"')
    script = f'''
        tell application "Messages"
            set targetService to 1st service whose service type = iMessage
            set targetBuddy to buddy "{handle}" of targetService
            send "{safe_msg}" to targetBuddy
        end tell
    '''
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True, timeout=15
    )
    if result.returncode != 0:
        print(f"  [send error] {result.stderr.strip()}")
    return result.returncode == 0


# ─── Receive ──────────────────────────────────────────────────────────────────

def _query_messages(handle: str, since_mac_ns: int, limit: int = 20) -> list[dict]:
    """
    Read messages from chat.db that:
      - came FROM the given handle (is_from_me = 0)
      - arrived after since_mac_ns
    Returns list of dicts: {text, timestamp, is_from_me}
    """
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute("""
            SELECT m.text, m.date, m.is_from_me
            FROM   message m
            JOIN   handle  h ON m.handle_id = h.ROWID
            WHERE  h.id         = ?
              AND  m.date        > ?
              AND  m.is_from_me  = 0
              AND  m.text        IS NOT NULL
              AND  m.text        != ''
            ORDER  BY m.date ASC
            LIMIT  ?
        """, (handle, since_mac_ns, limit))
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


def wait_for_responses(
    sent_at: datetime,
    count: int = 1,
    timeout: int = 150,
    handle: str = ASMI_HANDLE,
) -> list[str]:
    """
    Wait up to `timeout` seconds for `count` responses from Asmi after `sent_at`.
    Returns list of response texts (may be fewer than `count` if timeout reached).
    """
    since_ns   = _mac_ts(sent_at)
    deadline   = time.time() + timeout
    collected  = []

    print(f"  Waiting for {count} response(s) (timeout={timeout}s)…", end="", flush=True)
    while time.time() < deadline:
        msgs = _query_messages(handle, since_ns, limit=count * 3)
        # Deduplicate by timestamp
        seen_ts   = {m["timestamp"] for m in collected} if collected else set()
        new_msgs  = [m for m in msgs if m["timestamp"] not in seen_ts]
        if new_msgs:
            for m in new_msgs:
                collected.append(m)
                print(f"\n  ✓ Got response [{len(collected)}/{count}]: {m['text'][:80]}…")
        if len(collected) >= count:
            break
        print(".", end="", flush=True)
        time.sleep(POLL_INTERVAL)

    if len(collected) < count:
        print(f"\n  ⚠ Timeout — got {len(collected)}/{count} responses")
    else:
        print()

    return [m["text"] for m in collected]


def send_and_wait(
    message: str,
    count: int = 1,
    timeout: int = 150,
    handle: str = ASMI_HANDLE,
) -> list[str]:
    """Send one message and wait for `count` responses. Returns response texts."""
    sent_at = datetime.now(timezone.utc)
    ok = send_imessage(message, handle)
    if not ok:
        print(f"  [!] Failed to send: {message[:60]}")
        return []
    return wait_for_responses(sent_at, count=count, timeout=timeout, handle=handle)


def send_burst(
    messages: list[str],
    burst_delay: float = 1.0,
    expected_responses: int = None,
    timeout: int = 240,
    handle: str = ASMI_HANDLE,
) -> list[str]:
    """
    Send multiple messages rapidly (burst_delay seconds apart),
    then collect all responses.
    Returns list of response texts.
    """
    if expected_responses is None:
        expected_responses = len(messages)

    sent_at = datetime.now(timezone.utc)
    for i, msg in enumerate(messages):
        print(f"  → Sending [{i+1}/{len(messages)}]: {msg[:70]}")
        send_imessage(msg, handle)
        if i < len(messages) - 1:
            time.sleep(burst_delay)

    return wait_for_responses(
        sent_at,
        count=expected_responses,
        timeout=timeout,
        handle=handle,
    )


def send_sequence(
    messages: list[str],
    sequence_delay: float = 12.0,
    timeout_per: int = 120,
    handle: str = ASMI_HANDLE,
) -> list[str]:
    """
    Send messages one at a time, waiting for a response to each before sending the next.
    Returns list of all responses in order.
    """
    all_responses = []
    for i, msg in enumerate(messages):
        print(f"\n  → Step [{i+1}/{len(messages)}]: {msg[:70]}")
        sent_at = datetime.now(timezone.utc)
        ok = send_imessage(msg, handle)
        if not ok:
            all_responses.append(None)
            continue
        responses = wait_for_responses(sent_at, count=1, timeout=timeout_per, handle=handle)
        all_responses.append(responses[0] if responses else None)
        if i < len(messages) - 1:
            print(f"  (waiting {sequence_delay}s before next message…)")
            time.sleep(sequence_delay)
    return all_responses
