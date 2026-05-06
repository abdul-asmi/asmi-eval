# Asmi iMessage Eval System

Automated regression testing for Asmi's iMessage interface. Sends real iMessages to Asmi, captures responses via `chat.db`, and uses Gemini as an LLM judge to pass/fail each test. Outputs an HTML report.

---

## Table of Contents

1. [One-Time Setup](#one-time-setup)
2. [Running Evals from Terminal](#running-evals-from-terminal)
3. [Re-Judging Without Resending Messages](#re-judging-without-resending-messages)
4. [Editing Test Cases — Browser UI](#editing-test-cases--browser-ui)
5. [Command Daemon — Run from iPhone](#command-daemon--run-from-iphone)
6. [Config Reference](#config-reference)
7. [Test Case Structure](#test-case-structure)
8. [Categories](#categories)
9. [Output Files](#output-files)
10. [How It Works](#how-it-works)
11. [Troubleshooting](#troubleshooting)

---

## One-Time Setup

### 1. Grant Terminal Full Disk Access

The eval system reads `~/Library/Messages/chat.db` to capture Asmi's responses. macOS blocks this by default.

```
System Settings → Privacy & Security → Full Disk Access → enable Terminal
```

> If running via daemon/LaunchAgent, also enable Full Disk Access for `python3` or the binary listed in the plist.

### 2. Install Dependencies

```bash
cd /Users/yaybeedee/Desktop/asmi/eval
pip install google-genai --break-system-packages
```

### 3. Verify Config

Open `config.py` and confirm:

```python
ASMI_HANDLE    = "+14082307921"               # Asmi's iMessage number
GEMINI_API_KEY = "AIzaSy..."                  # Your Gemini API key
GEMINI_MODEL   = "models/gemini-3.1-flash-lite-preview"
COMMAND_HANDLE = "abdulgaffoor1729@gmail.com" # Your Apple ID (for daemon)
```

`RAILWAY_URL` is now read from the environment and defaults to the deployed Railway app URL, so completed runs can sync back to the hosted UI automatically. Set `RAILWAY_URL=""` if you want to disable that remote sync for a local-only session.

---

## Running Evals from Terminal

All commands run from the `eval/` directory:

```bash
cd /Users/yaybeedee/Desktop/asmi/eval
```

### Run all 28 tests

```bash
python run_eval.py
```

### Run a single category

```bash
python run_eval.py --category call_dedup
python run_eval.py --category sticky_message
python run_eval.py --category call_summary
python run_eval.py --category language_pref
python run_eval.py --category location_memory
python run_eval.py --category onboarding
python run_eval.py --category capability
python run_eval.py --category threep_nudge
```

### Run a single test by ID

```bash
python run_eval.py --id dedup_01
python run_eval.py --id sticky_03
```

### List all test IDs

```bash
python run_eval.py --list
```

Output format:
```
ID              CATEGORY             NAME
──────────────────────────────────────────────────────────────────────
dedup_01        call_dedup           Exact duplicate message dedup
sticky_01       sticky_message       Sticky task survives context switch
...
Total: 28 tests
```

Tests marked `[needs fresh acct]` have a `precondition` — read it before running.

### Skip HTML report generation

```bash
python run_eval.py --no-report
```

### Open the HTML report

After any run, a report file is created in the eval folder:

```bash
open report_20260504_1402.html
```

---

## Re-Judging Without Resending Messages

Use this when:
- The run completed but Gemini calls failed mid-way
- You want to change `pass_criteria` and re-evaluate existing results
- Context bleeding caused wrong verdicts (Gemini re-evaluates using the full response pool)

```bash
# Re-judge all tests in an existing results file
python rejudge.py results_20260504_1402.json

# Re-judge a single category
python rejudge.py results_20260504_1402.json --category call_dedup

# Re-judge a single test
python rejudge.py results_20260504_1402.json --id dedup_01
```

No iMessages are sent. Gemini sees all responses from the entire run to find which response actually answers each question (handles context bleeding across 28 tests in one thread).

Output: `results_rejudged_TIMESTAMP.json` + `report_rejudged_TIMESTAMP.html`

---

## Editing Test Cases — Browser UI

Instead of editing `test_cases.py` by hand, use the web interface:

```bash
cd /Users/yaybeedee/Desktop/asmi/eval
python ui.py
```

Then open: **http://localhost:8765**

If you want the UI server to restart automatically when `ui.py` changes, run:

```bash
cd /Users/yaybeedee/Desktop/asmi/eval
python watch_ui.py
```

### What you can do in the UI

- **Search** test cases by name or message content
- **Filter** by category or test type
- **Edit** any field inline — name, category, type, messages, wait time, pass criteria, etc.
- **Add** a new test case with a blank form
- **Delete** a test case
- **Save all** writes directly back to `test_cases.py`

> Press `Ctrl+C` in terminal to stop the UI server.

---

## Command Daemon — Run from iPhone

The daemon listens for iMessages you send to yourself and runs eval commands. You get results back in the same Messages thread.

### First-time setup (one command)

```bash
cd /Users/yaybeedee/Desktop/asmi/eval
bash setup_daemon.sh
```

This installs the daemon as a macOS LaunchAgent. It starts automatically on login and restarts if it crashes.

### Manual start / stop

```bash
# Start manually (foreground, shows logs)
python daemon.py

# Start in background
nohup python daemon.py > daemon.log 2>&1 &

# Stop background daemon
pkill -f daemon.py

# View live logs
tail -f daemon.log
```

### LaunchAgent control

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.asmi.eval.daemon.plist

# Start
launchctl load ~/Library/LaunchAgents/com.asmi.eval.daemon.plist

# Uninstall completely
launchctl unload ~/Library/LaunchAgents/com.asmi.eval.daemon.plist
rm ~/Library/LaunchAgents/com.asmi.eval.daemon.plist
```

### Commands — send from iPhone via iMessage

iMessage **abdulgaffoor1729@gmail.com** from your iPhone. All commands start with `!`.

| Command | What it does |
|---|---|
| `!help` | Show all available commands |
| `!run all` | Run the full 28-test suite |
| `!run call_dedup` | Run one category |
| `!run dedup_01` | Run one specific test by ID |
| `!rejudge` | Re-judge the most recent results file (no new messages) |
| `!status` | Summary of the last run — pass rate, failures |
| `!list` | List all 28 test IDs grouped by category |
| `!add test [description]` | Gemini auto-generates a new test case from your description |

**Examples:**

```
!run call_dedup
!run dedup_01
!run all
!status
!rejudge
!add test - when Asmi calls a business and gets voicemail, it should report honestly
```

The daemon also understands natural-language variations (e.g. "run the dedup tests" → `!run call_dedup`).

---

## Config Reference

File: `config.py`

| Setting | Default | Description |
|---|---|---|
| `ASMI_HANDLE` | `+14082307921` | Asmi's iMessage number |
| `GEMINI_API_KEY` | `AIzaSy...` | Gemini API key (AI Studio) |
| `GEMINI_MODEL` | `models/gemini-3.1-flash-lite-preview` | Gemini model to use |
| `RESPONSE_TIMEOUT` | `150` | Seconds to wait for a single response |
| `BURST_WAIT` | `240` | Seconds to wait when expecting multiple responses |
| `POLL_INTERVAL` | `3` | Seconds between `chat.db` polls |
| `BURST_SEND_DELAY` | `1.0` | Seconds between messages in a burst |
| `SEQUENCE_DELAY` | `12.0` | Seconds between messages in a sequence |
| `COMMAND_HANDLE` | `abdulgaffoor1729@gmail.com` | Your Apple ID for daemon commands |
| `COMMAND_PREFIX` | `!` | Prefix for daemon commands |
| `DAEMON_POLL` | `5` | Seconds between daemon inbox checks |

---

## Test Case Structure

Each entry in `test_cases.py` is a Python dict. Fields:

```python
{
    "id":          "category_XX",          # unique, e.g. "dedup_01"
    "name":        "Short descriptive name",
    "category":    "call_dedup",           # see categories below
    "type":        "single",               # see types below

    # For type = "single" or "dedup"
    "message":     "The exact iMessage to send to Asmi",

    # For type = "burst", "sequence", "burst_with_setup"
    "messages":    ["msg1", "msg2", "msg3"],

    # For type = "burst_with_setup"
    "setup_message": "Setup message to send first",

    "wait":            120,                # seconds to wait for response(s)
    "pass_criteria":   "What a passing response looks like — be specific.",
    "expected_responses": 2,              # optional, for burst/sequence/dedup
    "burst_delay":     1.0,              # optional, override burst send delay
    "sequence_delay":  12.0,             # optional, override sequence delay
    "setup_wait":      20,               # optional, wait after setup_message
    "manual_check":    "Also check X",   # optional, noted in report
    "precondition":    "Requires Y",     # optional, shown as warning at run time
    "note":            "Context note",   # optional, shown in report
}
```

### Test types

| Type | What it does |
|---|---|
| `single` | Sends one message, waits for one response |
| `burst` | Sends N messages rapidly, expects N responses |
| `burst_with_setup` | Sends a setup message first, waits, then bursts N more |
| `sequence` | Sends one message, waits for reply, then sends the next |
| `dedup` | Sends the same message twice, expects only ONE response |

---

## Categories

| Category | What it tests |
|---|---|
| `sticky_message` | Asmi remembers context across follow-up messages |
| `call_dedup` | Asmi deduplicates identical or near-identical requests |
| `call_summary` | Asmi gives accurate, honest summaries of phone calls |
| `language_pref` | Asmi respects language preferences set by the user |
| `location_memory` | Asmi remembers location context (city, preferences) |
| `onboarding` | First-time user experience and setup flows |
| `capability` | Asmi correctly handles in-scope and out-of-scope tasks |
| `threep_nudge` | Third-party referral and upgrade nudge behaviour |

---

## Output Files

Each run produces two files in the `eval/` directory:

| File | Description |
|---|---|
| `results_YYYYMMDD_HHMM.json` | Raw results — all test data, verdicts, responses |
| `report_YYYYMMDD_HHMM.html` | Visual HTML report — open in browser |
| `results_rejudged_TIMESTAMP.json` | Output from `rejudge.py` |
| `report_rejudged_TIMESTAMP.html` | Report from `rejudge.py` |
| `daemon.log` | Daemon stdout/stderr (when running as LaunchAgent) |

---

## How It Works

### Two-phase runner (avoids Gemini rate limits)

**Phase 1 — Collect:** Sends all iMessages, waits for and records all responses. No Gemini calls.

**Phase 2 — Judge:** After all responses are in, fires one Gemini call per test with a 4-second delay between each. Stays within Gemini free tier (~15 RPM).

### Context-aware judging

All 28 tests run in the same iMessage thread, so Asmi's response to test N can arrive during test N+1's window. The judge solves this by receiving the *entire response pool* from the whole run and finding which response actually answers each specific question, rather than blindly evaluating whatever was captured in a time window.

### `chat.db` polling

Asmi's responses are read directly from the macOS Messages database at `~/Library/Messages/chat.db` using a read-only SQLite connection. The system polls every 3 seconds and uses Mac Absolute Time (nanoseconds since 2001-01-01 UTC) to only read messages received after the test started.

---

## Troubleshooting

**"unable to open database file" / chat.db permission error**
→ Terminal needs Full Disk Access: `System Settings → Privacy & Security → Full Disk Access`

**Gemini 404 model error**
→ Update `GEMINI_MODEL` in `config.py`. List available models:
```python
import google.genai as genai
c = genai.Client(api_key="YOUR_KEY")
for m in c.models.list(): print(m.name)
```

**All tests returning UNCLEAR**
→ Gemini rate limit hit. The 4s delay should handle free tier. If still failing, increase `JUDGE_DELAY` in `runner.py` and `rejudge.py`.

**Responses captured as empty**
→ Check that `ASMI_HANDLE` in `config.py` matches the exact number in Messages. Open Messages, right-click the Asmi thread → Info to confirm.

**Daemon not receiving commands**
→ Make sure you're iMessaging `abdulgaffoor1729@gmail.com` (your Apple ID), not a phone number. The thread must show as a blue bubble (iMessage), not green (SMS).

**Daemon installed but not starting on login**
→ Check the plist: `cat ~/Library/LaunchAgents/com.asmi.eval.daemon.plist`
→ Check logs: `tail -f /Users/yaybeedee/Desktop/asmi/eval/daemon.log`

**Test with `precondition` fails immediately**
→ These tests require a specific state (e.g. a fresh account). Read the precondition note printed at run time and set up the required state first.
