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
    stop_file = os.environ.get("ASMI_STOP_FILE", "").strip()
    if stop_file and os.path.exists(stop_file):
        print("  ⏹ Stop requested — not sending another iMessage")
        return False
    handle = _resolve_handle(handle)
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
        cur.execute("""
            SELECT m.text, m.date, m.is_from_me
            FROM   message m
            JOIN   handle  h ON m.handle_id = h.ROWID
            WHERE  h.id         = ?
              AND  m.date        > ?
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
    seen_keys     = set()
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


