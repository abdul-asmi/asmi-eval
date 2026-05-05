#!/usr/bin/env python3
# ─── Asmi Eval Command Daemon ─────────────────────────────────────────────────
# Listens for iMessage commands from yourself, executes eval operations,
# and sends results back via iMessage.
#
# Setup:
#   1. On your iPhone, open Messages → New Message → abdulgaffoor1729@gmail.com
#   2. Run this daemon:  python daemon.py
#   3. Send commands like:  !run call_dedup  or  !status  or  !help
#   4. Replies come back to you at abdulgaffoor1729@gmail.com automatically
#
# To run in background:  nohup python daemon.py > daemon.log 2>&1 &
# To stop:               pkill -f daemon.py

import glob
import json
import os
import sqlite3
import ssl
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone, timedelta

_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import (
    CHAT_DB, COMMAND_HANDLE, COMMAND_PREFIX,
    DAEMON_POLL, ASMI_HANDLE, EVAL_DIR
)
try:
    from config import RAILWAY_URL
except ImportError:
    RAILWAY_URL = ""
from commands import handle
from imessage import send_imessage

_MAC_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)


def _mac_ts(dt: datetime) -> int:
    dt = dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return int((dt - _MAC_EPOCH).total_seconds() * 1_000_000_000)


def _get_new_commands(since_ns: int) -> list[dict]:
    """
    Poll chat.db for new messages sent FROM the command handle TO us (is_from_me=0).
    These are messages the user sent to themselves — treated as commands.
    """
    try:
        conn = sqlite3.connect(f"file:{CHAT_DB}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        # Messages FROM the command handle that arrived after since_ns
        # is_from_me=0 means received (the "other side" of the self-chat)
        cur.execute("""
            SELECT m.text, m.date, m.guid
            FROM   message m
            JOIN   handle  h ON m.handle_id = h.ROWID
            WHERE  h.id        = ?
              AND  m.date       > ?
              AND  m.is_from_me = 0
              AND  m.text       IS NOT NULL
              AND  m.text       != ''
            ORDER  BY m.date ASC
        """, (COMMAND_HANDLE, since_ns))

        rows = cur.fetchall()
        conn.close()

        return [{"text": row["text"], "date": row["date"], "guid": row["guid"]}
                for row in rows]
    except Exception as e:
        print(f"  [chat.db error] {e}")
        return []


def _is_command(text: str) -> bool:
    """Check if a message looks like a command."""
    t = text.strip().lower()
    if COMMAND_PREFIX and t.startswith(COMMAND_PREFIX.lower()):
        return True
    # Also accept natural-language commands without prefix
    keywords = ["run ", "rejudge", "status", "list", "help", "add test", "!"]
    return any(t.startswith(k) for k in keywords)


def _send_reply(text: str):
    """Send reply back to the command handle (yourself)."""
    send_imessage(text, handle=COMMAND_HANDLE)


def _poll_railway() -> dict | None:
    """Check Railway UI for a pending run request. Returns run dict or None."""
    if not RAILWAY_URL:
        print("  [railway] RAILWAY_URL not set — skipping poll")
        return None
    try:
        req = urllib.request.Request(f"{RAILWAY_URL}/api/poll", method="GET")
        with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as resp:
            data = json.loads(resp.read())
            return data.get("run")
    except Exception as e:
        print(f"  [railway poll error] {e}")
        return None


def _latest_results_json() -> list:
    """Read the exact results file written by the most recent run."""
    try:
        # Primary: read the pointer file written by commands.py after each run
        pointer = os.path.join(EVAL_DIR, ".latest_results_path")
        chosen = None
        if os.path.exists(pointer):
            with open(pointer) as f:
                candidate = f.read().strip()
            if os.path.exists(candidate):
                chosen = candidate
                print(f"  [results] using pointer → {os.path.basename(chosen)}")

        # Fallback: newest file by mtime
        if not chosen:
            files = glob.glob(os.path.join(EVAL_DIR, "results_*.json"))
            if not files:
                print("  [results] no results_*.json files found")
                return []
            files.sort(key=os.path.getmtime, reverse=True)
            chosen = files[0]
            print(f"  [results] fallback mtime → {os.path.basename(chosen)}")

        with open(chosen, "r", encoding="utf-8") as f:
            data = json.load(f)
        print(f"  [results] loaded {len(data)} entries from {os.path.basename(chosen)}")
        return data
    except Exception as e:
        print(f"  [results read error] {e}")
        return []


def _post_output_to_railway(output: str, status: str = "done"):
    """Send run output + results JSON back to Railway UI."""
    if not RAILWAY_URL:
        return
    try:
        results = _latest_results_json() if status == "done" else []
        body = json.dumps({
            "output":  output,
            "status":  status,
            "results": results,
        }).encode()
        req = urllib.request.Request(
            f"{RAILWAY_URL}/api/output",
            data=body, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        print(f"  [railway] posted output + {len(results)} results")
    except Exception as e:
        print(f"  [railway output post error] {e}")


def run():
    since_ns    = _mac_ts(datetime.now(timezone.utc))
    processed   = set()

    print(f"""
╔══════════════════════════════════════════════════════╗
  Asmi Eval Daemon — running
  Listening on: {COMMAND_HANDLE}
  Command prefix: "{COMMAND_PREFIX}"
  Poll interval: {DAEMON_POLL}s
  Ctrl+C to stop
╚══════════════════════════════════════════════════════╝
""")
    print(f"  Commands via iMessage to {COMMAND_HANDLE}:")
    print(f"    {COMMAND_PREFIX}help       → see all commands")
    print(f"    {COMMAND_PREFIX}run all    → run full test suite")
    print(f"    {COMMAND_PREFIX}status     → last run summary\n")

    while True:
        try:
            messages = _get_new_commands(since_ns)

            for msg in messages:
                guid = msg["guid"]
                if guid in processed:
                    continue

                text = msg["text"].strip()
                processed.add(guid)
                since_ns = max(since_ns, msg["date"])

                if not _is_command(text):
                    continue

                ts = datetime.now().strftime("%H:%M:%S")
                print(f"\n  [{ts}] Command received: {text[:80]}")

                # Strip prefix before handling
                clean = text.lstrip(COMMAND_PREFIX).strip() if COMMAND_PREFIX else text

                # Execute
                try:
                    response = handle(clean)
                except Exception as e:
                    response = f"❌ Error executing command: {e}"

                print(f"  → Reply ({len(response)} chars): {response[:120]}")

            # Check Railway UI for run requests triggered from the browser
            pending = _poll_railway()
            if pending:
                cat = pending.get("category")
                rid = pending.get("id")
                cmd = f"run {rid or cat or 'all'}"
                ts  = datetime.now().strftime("%H:%M:%S")
                print(f"\n  [{ts}] UI run request: {cmd}")
                try:
                    response = handle(cmd)
                except Exception as e:
                    response = f"❌ Error: {e}"
                # Post full output + HTML report to Railway UI for display in browser
                _post_output_to_railway(response, status="done")

        except KeyboardInterrupt:
            print("\n\n  Daemon stopped.")
            break
        except Exception as e:
            print(f"  [daemon error] {e}")

        time.sleep(DAEMON_POLL)


if __name__ == "__main__":
    run()
