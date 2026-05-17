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

from config import CHAT_DB, ASMI_HANDLE as _CFG_ASMI_HANDLE, POLL_INTERVAL, SILENCE_AFTER

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


def send_imessage(message: str, handle: str | None = None) -> bool:
    """Send an iMessage using AppleScript. Returns True on success."""
    handle = _resolve_handle(handle)
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

def _query_messages(handle: str, since_mac_ns: int, limit: int = 10) -> list[dict]:
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
    handle: str | None = None,
    max_responses: int = 10,
    drain_all: bool = False,
    return_raw: bool = False,
    silence_after: float = SILENCE_AFTER,
) -> list[str]:
    """
    Wait up to `timeout` seconds for `count` responses from Asmi after `sent_at`.
    Collect up to `max_responses` replies that arrive after the user message.
    Returns list of response texts (may be fewer than `count` if timeout reached).
    """
    handle = _resolve_handle(handle)
    since_ns      = _mac_ts(sent_at)
    deadline      = time.time() + timeout
    collected     = []
    last_new_time = None
    seen_keys     = set()

    print(f"  Waiting for {count} response(s) (timeout={timeout}s)…", end="", flush=True)
    while time.time() < deadline:
        msgs = _query_messages(handle, since_ns, limit=max_responses)
        new_msgs  = []
        for m in msgs:
            key = (m["timestamp"].isoformat(), m["text"])
            if key in seen_keys:
                continue
            seen_keys.add(key)
            new_msgs.append(m)
        if new_msgs:
            for m in new_msgs:
                collected.append(m)
                print(f"\n  ✓ Got response [{len(collected)}/{count}]: {m['text'][:80]}…")
            last_new_time = time.time()
        if len(collected) >= max_responses:
            break
        # If we have received at least one response, ensure we always wait
        # `silence_after` seconds after the last response before ending capture,
        # even if the original timeout would have ended earlier.
        if last_new_time is not None:
            deadline = max(deadline, last_new_time + silence_after)
        if last_new_time is not None and time.time() - last_new_time >= silence_after:
            if drain_all or len(collected) >= count:
                break
        print(".", end="", flush=True)
        time.sleep(POLL_INTERVAL)

    if len(collected) < count:
        print(f"\n  ⚠ Timeout — got {len(collected)}/{count} responses")
    else:
        print()

    if return_raw:
        return collected[:max_responses]
    return [m["text"] for m in collected[:max_responses]]


def send_and_wait(
    message: str,
    count: int = 1,
    timeout: int = 150,
    handle: str | None = None,
) -> list[str]:
    """Send one message and wait for `count` responses. Returns response texts."""
    sent_at = datetime.now(timezone.utc)
    ok = send_imessage(message, handle)
    if not ok:
        print(f"  [!] Failed to send: {message[:60]}")
        return []
    return wait_for_responses(sent_at, count=count, timeout=timeout, handle=handle, max_responses=10)


def send_burst(
    messages: list[str],
    burst_delay: float = 1.0,
    expected_responses: int = None,
    timeout: int = 240,
    handle: str | None = None,
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
        max_responses=10,
    )


def send_sequence(
    messages: list[str],
    sequence_delay: float = 12.0,
    timeout_per: int = 120,
    handle: str | None = None,
) -> list[str]:
    """
    Send messages one at a time, waiting for a response to each before sending the next.
    Returns list of all responses in order.
    """
    all_responses = []
    session_start = None
    seen_keys = set()
    for i, msg in enumerate(messages):
        print(f"\n  → Step [{i+1}/{len(messages)}]: {msg[:70]}")
        sent_at = datetime.now(timezone.utc)
        if session_start is None:
            session_start = sent_at
        ok = send_imessage(msg, handle)
        if not ok:
            all_responses.append(None)
            continue
        if i == len(messages) - 1:
            msgs = wait_for_responses(
                session_start,
                count=1,
                timeout=timeout_per,
                handle=handle,
                max_responses=10,
                drain_all=True,
                return_raw=True,
                silence_after=8.0,
            )
            new_msgs = []
            for m in msgs:
                key = (m["timestamp"].isoformat(), m["text"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_msgs.append(m)
            if new_msgs:
                all_responses.extend(m["text"] for m in new_msgs)
            else:
                all_responses.append(None)
        else:
            msgs = wait_for_responses(
                session_start,
                count=1,
                timeout=timeout_per,
                handle=handle,
                max_responses=10,
                drain_all=True,
                return_raw=True,
                silence_after=8.0,
            )
            new_msgs = []
            for m in msgs:
                key = (m["timestamp"].isoformat(), m["text"])
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                new_msgs.append(m)
            all_responses.extend(m["text"] for m in new_msgs)
        if i < len(messages) - 1:
            print(f"  (waiting {sequence_delay}s before next message…)")
            time.sleep(sequence_delay)
    return all_responses
