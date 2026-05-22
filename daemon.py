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
import re as _re_mod
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone, timedelta

_SSL_CTX = ssl.create_default_context()
_LAST_POLL_ERR_AT = 0.0

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from config import (
    CHAT_DB, COMMAND_HANDLE, COMMAND_PREFIX,
    DAEMON_POLL, ASMI_HANDLE, EVAL_DIR, REPORTS_DIR,
    RAILWAY_URL, LOCAL_UI_URL, DAEMON_TOKEN, DAEMON_OWNER_USER_ID,
)
from commands import handle
from imessage import send_imessage, _mac_ts
from test_case_store import load_test_cases as _load_test_cases
from slack import (
    get_bot_channels, poll_slack_commands_multi,
    post_message_to_slack, get_latest_slack_ts,
)

ASMI_TARGET_HANDLES = {
    "dev": "+14082307921",
    "prod": "+14082303488",
}

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


def _get_new_commands_safe(since_ns: int, timeout_s: float = 1.0) -> list[dict]:
    """Best-effort inbox poll that cannot stall the daemon loop."""
    result = {"rows": []}
    done = threading.Event()

    def worker():
        try:
            result["rows"] = _get_new_commands(since_ns)
        finally:
            done.set()

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    done.wait(timeout_s)
    return result["rows"] if done.is_set() else []


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


def _log_poll_error_throttled(label: str, url: str, err: Exception):
    """Avoid spamming identical poll errors every 5 seconds."""
    global _LAST_POLL_ERR_AT
    now = time.time()
    if now - _LAST_POLL_ERR_AT >= 20:
        _LAST_POLL_ERR_AT = now
        print(f"  [{label} poll error] {url} -> {err}")


def _poll_railway_full() -> dict:
    """Poll /api/poll — tries remote UI first, then local UI fallback."""
    if RAILWAY_URL:
        for timeout_s in (6, 10):
            try:
                req = urllib.request.Request(f"{RAILWAY_URL}/api/poll", method="GET")
                if DAEMON_TOKEN:
                    req.add_header("X-Daemon-Token", DAEMON_TOKEN)
                if DAEMON_OWNER_USER_ID:
                    req.add_header("X-Owner-User-Id", DAEMON_OWNER_USER_ID)
                with urllib.request.urlopen(req, timeout=timeout_s, context=_SSL_CTX) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                body = ""
                try:
                    body = e.read().decode("utf-8", errors="ignore")
                except Exception:
                    body = ""
                if e.code in (401, 403) and "Invalid daemon token" in body:
                    _log_poll_error_throttled(
                        "remote-auth",
                        RAILWAY_URL,
                        RuntimeError(
                            "Invalid daemon token. Update DAEMON_TOKEN in local .env.local to match Render."
                        ),
                    )
                elif e.code in (401, 403):
                    _log_poll_error_throttled(
                        "remote-auth",
                        RAILWAY_URL,
                        RuntimeError(f"Unauthorized ({e.code}). Check DAEMON_TOKEN and DAEMON_OWNER_USER_ID."),
                    )
                else:
                    _log_poll_error_throttled("remote", RAILWAY_URL, e)
            except Exception as e:
                _log_poll_error_throttled("remote", RAILWAY_URL, e)
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
    return bool(data.get("stop") or data.get("stop_current"))


def _ack_run_to_server(run: dict):
    """Tell the UI server that the daemon has claimed the pending run."""
    payload = json.dumps(run).encode()
    for url in filter(None, [RAILWAY_URL, LOCAL_UI_URL]):
        try:
            req = urllib.request.Request(f"{url}/api/ack-run", data=payload, method="POST")
            req.add_header("Content-Type", "application/json")
            if url == RAILWAY_URL:
                if DAEMON_TOKEN:
                    req.add_header("X-Daemon-Token", DAEMON_TOKEN)
                if DAEMON_OWNER_USER_ID:
                    req.add_header("X-Owner-User-Id", DAEMON_OWNER_USER_ID)
            ctx = _SSL_CTX if url.startswith("https") else None
            urllib.request.urlopen(req, timeout=4, context=ctx)
        except Exception:
            pass


_progress_state = {"current_test": None, "current_category": None, "completed": 0, "total": 0}
_current_run_id: str | None = None

# test_id → category map (refreshed before each run)
_TC_MAP = {}


def _refresh_tc_map():
    global _TC_MAP
    try:
        _TC_MAP = {t.get("id"): t.get("category") for t in _load_test_cases() if t.get("id")}
    except Exception:
        _TC_MAP = {}

def _push_logs_to_ui():
    """Read the last 300 lines of daemon.log and push it to the UI server."""
    log_path = os.path.join(EVAL_DIR, "daemon.log")
    if not os.path.exists(log_path):
        return
    try:
        with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
            lines = f.readlines()
            logs = "".join(lines[-300:])
        
        payload = json.dumps({"logs": logs}).encode()
        
        for url in filter(None, [RAILWAY_URL, LOCAL_UI_URL]):
            try:
                req = urllib.request.Request(f"{url}/api/debug/logs", data=payload, method="POST")
                req.add_header("Content-Type", "application/json")
                if url == RAILWAY_URL:
                    if DAEMON_TOKEN:
                        req.add_header("X-Daemon-Token", DAEMON_TOKEN)
                    if DAEMON_OWNER_USER_ID:
                        req.add_header("X-Owner-User-Id", DAEMON_OWNER_USER_ID)
                ctx = _SSL_CTX if url.startswith("https") else None
                urllib.request.urlopen(req, timeout=4, context=ctx)
            except Exception:
                pass
    except Exception:
        pass


def _parse_progress_line(line: str, total: int):
    """Detect test start lines (format: '  [test_id] Name') and post progress."""
    global _progress_state
    
    if "  [ElevenLabs] conversation_id: " in line:
        convo_id = line.split("  [ElevenLabs] conversation_id: ")[-1].strip()
        _progress_state["conversation_id"] = convo_id
        _post_progress()
        return

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
    _progress_state.pop("conversation_id", None) # Clear conversation ID for the new test!
    # completed = tests we've *started* so far (updated after each "✓ Collected")
    _post_progress()


def _mark_test_done(line: str):
    """Increment completed count when a test finishes."""
    if "✓ Collected" in line or "⚠ Timeout" in line:
        _progress_state["completed"] = _progress_state.get("completed", 0) + 1
        _post_progress()


def _post_progress():
    """Post current progress dict to Railway and local UI."""
    payload_dict = {
        "run_id":          _current_run_id,
        "current_test":     _progress_state.get("current_test"),
        "current_category": _progress_state.get("current_category"),
        "completed":        _progress_state.get("completed", 0),
        "total":            _progress_state.get("total", 0),
    }
    if "conversation_id" in _progress_state:
        payload_dict["conversation_id"] = _progress_state["conversation_id"]
    payload = json.dumps(payload_dict).encode()

    for url in filter(None, [RAILWAY_URL, LOCAL_UI_URL]):
        try:
            req = urllib.request.Request(
                f"{url}/api/progress", data=payload, method="POST",
            )
            req.add_header("Content-Type", "application/json")
            if url == RAILWAY_URL:
                if DAEMON_TOKEN:
                    req.add_header("X-Daemon-Token", DAEMON_TOKEN)
                if DAEMON_OWNER_USER_ID:
                    req.add_header("X-Owner-User-Id", DAEMON_OWNER_USER_ID)
            ctx = _SSL_CTX if url.startswith("https") else None
            urllib.request.urlopen(req, timeout=4, context=ctx)
        except Exception:
            pass


def _run_with_stop(cmd: str, extra_env: dict | None = None) -> str:
    """
    Run an eval command via subprocess, polling for a stop signal every 5s.
    Kills the process and returns a ⏹ message if stop is requested.
    """
    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    if extra_env:
        env.update({k: str(v) for k, v in extra_env.items() if v is not None})
    old_snapshot = os.environ.get("ASMI_TEST_CASES_JSON")
    if env.get("ASMI_TEST_CASES_JSON"):
        os.environ["ASMI_TEST_CASES_JSON"] = env["ASMI_TEST_CASES_JSON"]

    arg = cmd.strip().removeprefix("run").strip()
    try:
        try:
            _all_tc_for_routing = _load_test_cases()
        except Exception:
            _all_tc_for_routing = []
        dynamic_categories = {t.get("category") for t in _all_tc_for_routing if t.get("category")}
        if not arg or arg == "all":
            proc_cmd = [sys.executable, "-u", "run_eval.py"]
            label = "full suite"
        elif "," in arg:
            parts = [p.strip() for p in arg.split(",") if p.strip()]
            if parts and all(p in dynamic_categories for p in parts):
                proc_cmd = [sys.executable, "-u", "run_eval.py", "--categories", arg]
                label = f"categories: {arg}"
            else:
                proc_cmd = [sys.executable, "-u", "run_eval.py", "--ids", arg]
                label = f"tests: {arg}"
        elif arg in dynamic_categories:
            proc_cmd = [sys.executable, "-u", "run_eval.py", "--category", arg]
            label = f"category: {arg}"
        else:
            proc_cmd = [sys.executable, "-u", "run_eval.py", "--id", arg]
            label = f"test: {arg}"

        # Refresh the in-memory id → category mapping so progress uses latest dashboard edits
        _refresh_tc_map()

        # Count total tests for progress reporting
        try:
            _all_tc = _all_tc_for_routing or _load_test_cases()
            if not arg or arg == "all":
                total_count = len(_all_tc)
            elif "," in arg:
                parts = [c.strip() for c in arg.split(',') if c.strip()]
                if parts and all(p in dynamic_categories for p in parts):
                    total_count = len([t for t in _all_tc if t["category"] in parts])
                else:
                    total_count = len([t for t in _all_tc if t["id"] in parts])
            elif arg in dynamic_categories:
                total_count = len([t for t in _all_tc if t["category"] == arg])
            else:
                total_count = 1
        except Exception:
            total_count = 0
    finally:
        if old_snapshot is None:
            os.environ.pop("ASMI_TEST_CASES_JSON", None)
        else:
            os.environ["ASMI_TEST_CASES_JSON"] = old_snapshot

    # Reset progress state for this run
    global _progress_state
    _progress_state = {"current_test": None, "current_category": None, "completed": 0, "total": total_count}
    _post_progress()

    stop_fd, stop_path = tempfile.mkstemp(prefix="asmi_eval_stop_", suffix=".flag")
    skip_fd, skip_path = tempfile.mkstemp(prefix="asmi_eval_skip_", suffix=".json")
    os.close(stop_fd)
    os.close(skip_fd)
    try:
        os.unlink(stop_path)
    except FileNotFoundError:
        pass
    with open(skip_path, "w", encoding="utf-8") as f:
        json.dump([], f)
    env["ASMI_STOP_FILE"] = stop_path
    env["ASMI_SKIP_FILE"] = skip_path
    if _current_run_id:
        env["ASMI_RUN_ID"] = _current_run_id

    proc = subprocess.Popen(
        proc_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True, bufsize=1, cwd=EVAL_DIR, env=env,
    )

    output_lines = []
    stop_flag = False
    requested_skips = set()

    def check_stop_periodically():
        """Background thread: mirror UI stop/skip signals into files for run_eval.py."""
        nonlocal stop_flag
        while not stop_flag:
            data = _poll_railway_full()
            skips = data.get("skip_ids") or []
            if skips:
                requested_skips.update(str(x).strip() for x in skips if str(x).strip())
                try:
                    with open(skip_path, "w", encoding="utf-8") as f:
                        json.dump(sorted(requested_skips), f)
                except Exception:
                    pass
            if data.get("stop") or data.get("stop_current"):
                if not os.path.exists(stop_path):
                    print(f"\n  [stop] graceful stop requested (pid={proc.pid}); will judge captured results")
                    try:
                        with open(stop_path, "w", encoding="utf-8") as f:
                            f.write("stop")
                    except Exception:
                        pass
            time.sleep(1)

    def post_output_periodically():
        """Background thread: periodically post accumulated output to the UI."""
        nonlocal stop_flag
        last_posted = ""
        while not stop_flag:
            time.sleep(2.0)
            if stop_flag:
                break
            current_output = "".join(output_lines)
            if current_output != last_posted:
                def _post_bg(out=current_output):
                    _post_output_to_local_ui(out, status="running")
                    _post_output_to_railway(out, status="running")
                threading.Thread(target=_post_bg, daemon=True).start()
                last_posted = current_output

    # Start background thread for stop checking
    stop_thread = threading.Thread(target=check_stop_periodically, daemon=True)
    stop_thread.start()

    # Start background thread for posting output
    post_thread = threading.Thread(target=post_output_periodically, daemon=True)
    post_thread.start()

    # Read output continuously without blocking delays
    try:
        while True:
            if stop_flag:
                break
            line = proc.stdout.readline()
            if not line:
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
        for path in (stop_path, skip_path):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    output = "".join(output_lines)
    m = _re_mod.search(r'Raw results:\s*(\S+results_\S+\.json)', output)
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
            if candidate:
                candidates = []
                if os.path.isabs(candidate):
                    candidates.append(candidate)
                else:
                    candidates.extend([
                        candidate,
                        os.path.join(REPORTS_DIR, candidate),
                        os.path.join(EVAL_DIR, candidate),
                    ])
                for path in candidates:
                    if os.path.exists(path):
                        chosen = path
                        print(f"  [results] using pointer → {os.path.basename(chosen)}")
                        break

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


def _latest_report_html() -> str:
    """Read the report HTML that matches the latest results pointer."""
    try:
        pointer = os.path.join(REPORTS_DIR, ".latest_results_path")
        chosen = None
        if os.path.exists(pointer):
            with open(pointer) as f:
                candidate = os.path.basename(f.read().strip())
            m = _re_mod.match(r"results_(.+)\.json$", candidate)
            if m:
                report_path = os.path.join(REPORTS_DIR, f"report_{m.group(1)}.html")
                if os.path.exists(report_path):
                    chosen = report_path

        if not chosen:
            report_files = glob.glob(os.path.join(REPORTS_DIR, "report_*.html"))
            if report_files:
                report_files.sort(key=os.path.getmtime, reverse=True)
                chosen = report_files[0]

        if not chosen:
            return ""
        with open(chosen, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _post_output_to_railway(output: str, status: str = "done"):
    """Send run output + results JSON back to Railway UI."""
    if not RAILWAY_URL:
        return
    try:
        results = _latest_results_json() if status == "done" else []
        report_html = _latest_report_html() if status == "done" else ""
        asmi_target = next((r.get("asmi_target") for r in results if isinstance(r, dict) and r.get("asmi_target")), "")
        asmi_handle = next((r.get("asmi_handle") for r in results if isinstance(r, dict) and r.get("asmi_handle")), "")
        body = json.dumps({
            "run_id": _current_run_id,
            "output":  output,
            "status":  status,
            "results": results,
            "report_html": report_html,
            "asmi_target": asmi_target,
            "asmi_handle": asmi_handle,
        }).encode()
        req = urllib.request.Request(
            f"{RAILWAY_URL}/api/output",
            data=body, method="POST",
        )
        req.add_header("Content-Type", "application/json")
        if DAEMON_TOKEN:
            req.add_header("X-Daemon-Token", DAEMON_TOKEN)
        if DAEMON_OWNER_USER_ID:
            req.add_header("X-Owner-User-Id", DAEMON_OWNER_USER_ID)
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
        report_html = _latest_report_html() if status == "done" else ""
        asmi_target = next((r.get("asmi_target") for r in results if isinstance(r, dict) and r.get("asmi_target")), "")
        asmi_handle = next((r.get("asmi_handle") for r in results if isinstance(r, dict) and r.get("asmi_handle")), "")
        body = json.dumps({
            "output":  output,
            "status":  status,
            "results": results,
            "report_html": report_html,
            "asmi_target": asmi_target,
            "asmi_handle": asmi_handle,
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

    # Initialize Slack command polling tracking
    slack_last_ts = {}
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if slack_token:
        print("  [Slack] Initializing channel timestamps...")
        try:
            bot_channels = get_bot_channels()
            for ch in bot_channels:
                latest_ts = get_latest_slack_ts(ch)
                if latest_ts:
                    slack_last_ts[ch] = latest_ts
            print(f"  [Slack] Listening on channels: {', '.join(bot_channels)}")
        except Exception as e:
            print(f"  [Slack] Initialization error: {e}")

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
    if RAILWAY_URL:
        print(f"  Remote UI: {RAILWAY_URL}")
    if LOCAL_UI_URL:
        print(f"  Local UI fallback: {LOCAL_UI_URL}\n")
    if not DAEMON_TOKEN:
        print("  ⚠ DAEMON_TOKEN is empty in local env. Remote Render poll will be rejected.")
    if not DAEMON_OWNER_USER_ID:
        print("  ⚠ DAEMON_OWNER_USER_ID is empty in local env. Owner-scoped run polling may fail.")

    poll_count = 0
    while True:
        try:
            poll_data = _poll_railway_full()
            poll_count += 1
            if poll_count % 2 == 0 or poll_count == 1:
                _push_logs_to_ui()
            if poll_count % 10 == 0:  # Log every 50 seconds (10 polls × 5s)
                has_run = bool(poll_data.get("run"))
                print(f"  [poll #{poll_count}] UI poll ok, pending_run={has_run}")

            # Check for run requests triggered from the browser
            if poll_data.get("stop"):
                pass  # already cleared by server; nothing running here
            pending = poll_data.get("run")
            if pending:
                global _current_run_id
                _current_run_id = (pending.get("run_id") or pending.get("id")) if isinstance(pending, dict) else None
                ids = pending.get("ids")
                cats = pending.get("categories")
                cat = pending.get("category")
                rid = pending.get("id")
                if not (ids or cats or cat or rid):
                    print("\n  [ui run request skipped] empty selection payload")
                    _post_output_to_local_ui("No tests selected. Please select at least one test and run again.", status="done")
                    time.sleep(DAEMON_POLL)
                    continue
                if ids:
                    cmd = f"run {','.join(ids)}"
                elif cats:
                    cmd = f"run {','.join(cats)}"
                else:
                    cmd = f"run {rid or cat or 'all'}"
                ts  = datetime.now().strftime("%H:%M:%S")
                target_key = (pending.get("asmi_target") or "").strip().lower() if isinstance(pending.get("asmi_target"), str) else ""
                target_handle = ASMI_TARGET_HANDLES.get(target_key) or pending.get("asmi_handle") or ""
                target_label = f" target={target_key or 'custom'}:{target_handle}" if target_handle else ""
                print(f"\n  [{ts}] UI run request: {cmd}{target_label}")
                _ack_run_to_server(pending)
                if poll_data.get("stop"):
                    response = "⏹ Stopped before sending any messages."
                else:
                    try:
                        response = _run_with_stop(cmd, extra_env={
                            "ASMI_INTERACTIVE_AUTO_CONTINUE": pending.get("interactive_auto_continue", "1"),
                            "ASMI_TARGET": target_key,
                            "ASMI_HANDLE": target_handle,
                            "ASMI_TEST_CASES_JSON": json.dumps(pending.get("test_cases") or []),
                        })
                    except Exception as e:
                        response = f"❌ Error: {e}"
                # Post full output + HTML report to Railway UI for display in browser
                final_status = "stopped" if response.startswith("⏹") else "done"
                _post_output_to_railway(response, status=final_status)
                # Also post results back to the local UI server so inline cards can render.
                _post_output_to_local_ui(response, status=final_status)
                _current_run_id = None

            messages = _get_new_commands_safe(since_ns)

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

            # ─── Poll Slack Commands ───
            if slack_token:
                try:
                    # Dynamically refresh joined channels in case the bot was added/removed
                    bot_channels = get_bot_channels()
                    for ch in bot_channels:
                        if ch not in slack_last_ts:
                            slack_last_ts[ch] = get_latest_slack_ts(ch)

                    # Remove channels we are no longer in
                    for ch in list(slack_last_ts.keys()):
                        if ch not in bot_channels:
                            slack_last_ts.pop(ch, None)

                    # Poll for commands
                    slack_cmds, updated_ts_dict = poll_slack_commands_multi(slack_last_ts)
                    slack_last_ts = updated_ts_dict

                    for smsg in slack_cmds:
                        text = smsg["text"].strip()
                        ch_id = smsg["channel_id"]
                        
                        clean = text.lstrip("!").strip()
                        ts_str = datetime.now().strftime("%H:%M:%S")
                        print(f"\n  [{ts_str}] Slack Command received from channel {ch_id}: {text[:80]}")

                        # Temporarily override SLACK_CHANNEL so runner and uploads go to this channel
                        old_slack_channel = os.environ.get("SLACK_CHANNEL")
                        os.environ["SLACK_CHANNEL"] = ch_id

                        try:
                            response = handle(clean)
                        except Exception as e:
                            response = f"❌ Error executing command: {e}"

                        # Restore SLACK_CHANNEL env var
                        if old_slack_channel is not None:
                            os.environ["SLACK_CHANNEL"] = old_slack_channel
                        else:
                            os.environ.pop("SLACK_CHANNEL", None)

                        print(f"  → Slack Reply ({len(response)} chars): {response[:120]}")
                        post_message_to_slack(response, channel_id=ch_id)
                except Exception as e:
                    print(f"  [Slack Command loop error] {e}")

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
