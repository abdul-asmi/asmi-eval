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
    DAEMON_POLL, ASMI_HANDLE, EVAL_DIR, REPORTS_DIR,
    RAILWAY_URL, LOCAL_UI_URL,
)
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
    return _poll_railway_full().get("run")


def _poll_railway_full() -> dict:
    """Poll /api/poll — tries Railway first, falls back to local UI."""
    # Try Railway
    if RAILWAY_URL:
        try:
            req = urllib.request.Request(f"{RAILWAY_URL}/api/poll", method="GET")
            with urllib.request.urlopen(req, timeout=5, context=_SSL_CTX) as resp:
                return json.loads(resp.read())
        except Exception as e:
            print(f"  [railway poll error] {e}")
    # Fall back to local UI
    if LOCAL_UI_URL:
        try:
            req = urllib.request.Request(f"{LOCAL_UI_URL}/api/poll", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return json.loads(resp.read())
        except Exception:
            pass
    return {}


def _check_stop() -> bool:
    """Return True if the server has a stop signal pending."""
    data = _poll_railway_full()
    return bool(data.get("stop"))


# Track progress state across calls
_progress_state = {"current_test": None, "current_category": None, "completed": 0, "total": 0}

# Build test_id → category map once at startup
try:
    from test_cases import TEST_CASES as _TC
    _TC_MAP = {t["id"]: t["category"] for t in _TC}
except Exception:
    _TC_MAP = {}

import re as _re_mod


def _parse_progress_line(line: str, total: int):
    """Detect test start lines (format: '  [test_id] Name') and post progress."""
    global _progress_state
    # Match lines like "  [sticky_01] Single research task…"
    # Must be a word_number pattern, not [1/28] judge lines
    m = _re_mod.match(r'^\s+\[([a-z][a-z0-9_]+)\]\s+\S', line)
    if not m:
        return
    test_id = m.group(1)
    # Skip judge lines like [1/28]
    if '/' in test_id:
        return
    category = _TC_MAP.get(test_id, "")
    _progress_state["current_test"]     = test_id
    _progress_state["current_category"] = category
    _progress_state["total"]            = total
    # completed = tests we've *started* so far (updated after each "✓ Collected")
    _post_progress()


def _mark_test_done(line: str):
    """Increment completed count when a test finishes."""
    if "✓ Collected" in line or "⚠ Timeout" in line:
        _progress_state["completed"] = _progress_state.get("completed", 0) + 1
        _post_progress()


def _post_progress():
    """Post current progress dict to Railway and local UI."""
    payload = json.dumps({
        "current_test":     _progress_state.get("current_test"),
        "current_category": _progress_state.get("current_category"),
        "completed":        _progress_state.get("completed", 0),
        "total":            _progress_state.get("total", 0),
    }).encode()

    for url in filter(None, [RAILWAY_URL, LOCAL_UI_URL]):
        try:
            req = urllib.request.Request(
                f"{url}/api/progress", data=payload, method="POST",
            )
            req.add_header("Content-Type", "application/json")
            ctx = _SSL_CTX if url.startswith("https") else None
            urllib.request.urlopen(req, timeout=4, context=ctx)
        except Exception:
            pass


def _run_with_stop(cmd: str) -> str:
    """
    Run an eval command via subprocess, polling for a stop signal every 5s.
    Kills the process and returns a ⏹ message if stop is requested.
    """
    import subprocess, sys, re as _re, threading
    from config import EVAL_DIR, REPORTS_DIR
    from commands import CATEGORIES

    arg = cmd.strip().removeprefix("run").strip()
    if not arg or arg == "all":
        proc_cmd = [sys.executable, "run_eval.py"]
        label = "full suite"
    elif arg in CATEGORIES:
        proc_cmd = [sys.executable, "run_eval.py", "--category", arg]
        label = f"category: {arg}"
    else:
        proc_cmd = [sys.executable, "run_eval.py", "--id", arg]
        label = f"test: {arg}"

    # Count total tests for progress reporting
    try:
        from test_cases import TEST_CASES as _all_tc
        if not arg or arg == "all":
            total_count = len(_all_tc)
        elif arg in CATEGORIES:
            total_count = len([t for t in _all_tc if t["category"] == arg])
        else:
            total_count = 1
    except Exception:
        total_count = 0

    # Reset progress state for this run
    global _progress_state
    _progress_state = {"current_test": None, "current_category": None, "completed": 0, "total": total_count}
    _post_progress()

    proc = subprocess.Popen(
        proc_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1, cwd=EVAL_DIR,
    )

    output_lines = []
    stop_flag = False

    def check_stop_periodically():
        """Background thread: check for stop signal every 5s."""
        nonlocal stop_flag
        while not stop_flag:
            if _check_stop():
                print(f"\n  [stop] killing run_eval.py (pid={proc.pid})")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                stop_flag = True
                break
            time.sleep(5)

    # Start background thread for stop checking
    stop_thread = threading.Thread(target=check_stop_periodically, daemon=True)
    stop_thread.start()

    # Read output continuously without blocking delays
    try:
        for line in proc.stdout:
            if stop_flag:
                break
            output_lines.append(line)
            print(line, end="", flush=True)
            _parse_progress_line(line, total_count)
            _mark_test_done(line)
    except Exception as e:
        print(f"  [read error] {e}")
    finally:
        stop_flag = True
        proc.wait()

    output = "".join(output_lines)
    m = _re.search(r'Raw results:\s*(\S+results_\S+\.json)', output)
    if m:
        try:
            with open(os.path.join(REPORTS_DIR, '.latest_results_path'), 'w') as f:
                f.write(m.group(1))
        except Exception:
            pass

    from commands import _extract_summary, _latest_report
    summary = _extract_summary(output)
    report  = _latest_report()
    return (
        f"Run complete — {label}\n"
        f"{summary}\n"
        f"Report: {report or 'check eval folder'}"
    )


def _latest_results_json() -> list:
    """Read the exact results file written by the most recent run."""
    try:
        # Primary: read the pointer file written by commands.py after each run
        pointer = os.path.join(REPORTS_DIR, ".latest_results_path")
        chosen = None
        if os.path.exists(pointer):
            with open(pointer) as f:
                candidate = f.read().strip()
            if os.path.exists(candidate):
                chosen = candidate
                print(f"  [results] using pointer → {os.path.basename(chosen)}")

        # Fallback: newest file by mtime in reports/
        if not chosen:
            files = glob.glob(os.path.join(REPORTS_DIR, "results_*.json"))
            if not files:
                print("  [results] no results_*.json files found in reports/")
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


def _post_output_to_local_ui(output: str, status: str = "done"):
    """Send run output + results JSON back to a local UI server."""
    if not LOCAL_UI_URL:
        return
    try:
        results = _latest_results_json() if status == "done" else []
        body = json.dumps({
            "output":  output,
            "status":  status,
            "results": results,
        }).encode()
        req = urllib.request.Request(
            f"{LOCAL_UI_URL}/api/output",
            data=body, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        urllib.request.urlopen(req, timeout=15, context=_SSL_CTX)
        print(f"  [local UI] posted output + {len(results)} results")
    except Exception as e:
        print(f"  [local UI output post error] {e}")


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
            # Heartbeat: poll UI to stay online
            poll_data = _poll_railway_full()

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

            # Check for run requests triggered from the browser
            if poll_data.get("stop"):
                pass  # already cleared by server; nothing running here
            pending = poll_data.get("run")
            if pending:
                cat = pending.get("category")
                rid = pending.get("id")
                cmd = f"run {rid or cat or 'all'}"
                ts  = datetime.now().strftime("%H:%M:%S")
                print(f"\n  [{ts}] UI run request: {cmd}")
                try:
                    response = _run_with_stop(cmd)
                except Exception as e:
                    response = f"❌ Error: {e}"
                # Post full output + HTML report to Railway UI for display in browser
                final_status = "stopped" if response.startswith("⏹") else "done"
                _post_output_to_railway(response, status=final_status)
                # Also post results back to the local UI server so inline cards can render.
                _post_output_to_local_ui(response, status=final_status)

        except KeyboardInterrupt:
            print("\n\n  Daemon stopped.")
            break
        except Exception as e:
            import traceback
            print(f"  [daemon error] {e}")
            traceback.print_exc()

        time.sleep(DAEMON_POLL)


if __name__ == "__main__":
    run()
