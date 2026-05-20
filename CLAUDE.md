# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

An iMessage eval system for testing **Asmi**, an AI personal assistant accessible via iMessage. The system sends real iMessages to Asmi's number, waits for responses, and uses Gemini as an LLM judge to score each test.

Requires macOS Terminal with **Full Disk Access** (System Settings → Privacy & Security → Full Disk Access) to read `~/Library/Messages/chat.db`.

## Setup

```bash
pip install google-generativeai
```

## Running tests

```bash
# Get latest changes first
git pull

# List all tests
python run_eval.py --list

# Run everything (~15–20 min)
python run_eval.py

# Run one category (~5 min for quick regression)
python run_eval.py --category sticky_message
python run_eval.py --category call_dedup

# Run one specific test
python run_eval.py --id sticky_03

# Open the generated HTML report
open report_YYYYMMDD_HHMM.html
```

## Shipping to Railway

Railway deploys from GitHub. Before starting work, `git pull`. After changes, `git push` so Railway can deploy the latest commit.

Important: local edits are not live until they are committed and pushed to `main`. When a user expects a fix to be live, do not stop after local file changes; either push the change or explicitly say it has not been pushed/deployed yet.

## Troubleshooting: run shows old report / doesn’t run

The web UI only queues a run; execution happens on the Mac runner that polls the UI.

- If you click **Run this test** and the inline card shows an older result, check the **Run Monitor**: if the item stays **Queued**, the runner hasn’t claimed the job yet.
- The server returns a `mac_online` flag when queueing; if it’s `false`, the UI will warn that it’s waiting for the runner heartbeat.
- A fresh run result only appears after the runner posts back to `/api/output` with `status=done` and `results`.

## Daemon restart (nohup)

When asked for the “daemon restart” command, use:

```bash
cd ~/Desktop/asmi-eval && pkill -f daemon.py; nohup python daemon.py > daemon.log 2>&1 &
```

## Re-judging existing results (no iMessages sent)

```bash
# Re-run Gemini judge on a previous results file with full response-pool context
python rejudge.py results_20260504_1402.json
python rejudge.py results_20260504_1402.json --category call_dedup
python rejudge.py results_20260504_1402.json --id dedup_01
```

Rejudge passes ALL responses from the full run to Gemini so it can match responses to the right test, compensating for out-of-order delivery across the shared iMessage thread.

## Remote command daemon

```bash
# Start the daemon (listens for iMessage commands sent to yourself)
python daemon.py

# Run in background
nohup python daemon.py > daemon.log 2>&1 &
pkill -f daemon.py   # to stop

# Then iMessage yourself at a.shaikriyaz123@gmail.com with commands like:
!run call_dedup
!status
!help
```

## Architecture

For the fuller run lifecycle, UI state rules, server restart notes, and current context, read `docs/SYSTEM_ARCHITECTURE.md` first. For a plain-English product map, read `docs/PRODUCT_MANAGER_ARCHITECTURE.md`.

```
Entry points (root):
  run_eval.py     CLI — parses args, calls runner, saves JSON + HTML
  daemon.py       iMessage command listener — runs eval ops from your phone
  ui.py           Web UI server (hosted on Render)
  rejudge.py      Re-scores existing results JSON without sending iMessages
  watch_ui.py     Auto-restarts ui.py on file changes (local dev)

Library modules (src/):
  config.py         Asmi's number, Gemini key, timeouts, daemon config
  runner.py         Orchestrates test types (single/burst/sequence/dedup)
  imessage.py       Sends via AppleScript, reads chat.db — do not edit
  judge.py          Gemini judge — judge() / judge_with_context()
  report.py         Generates HTML report — do not edit
  commands.py       Command handlers called by daemon.py
  test_cases.py     ALL test definitions — primary file to edit
  test_case_store.py  Loads test cases (local file or Supabase)
  supabase_helpers.py Supabase auth + storage helpers

Other:
  docs/           Reference docs and setup guide
  scripts/        Shell scripts for daemon/UI restart and LaunchAgent setup
  reports/        All results_*.json and report_*.html output files
  supabase/       DB schema
```

**The only two files edited regularly:** `src/test_cases.py` and `docs/EVAL_GUIDE.md`.

## Test types

| Type | Use when |
|---|---|
| `single` | One message, one response |
| `burst` | Rapid-fire N messages, expect N replies |
| `burst_with_setup` | Send setup message first, then burst |
| `sequence` | Multi-turn — each message waits for reply before next sends |
| `dedup` | Send same message twice, expect only 1 response back |

## Categories

`sticky_message` · `call_dedup` · `call_summary` · `language_pref` · `location_memory` · `onboarding` · `capability` · `threep_nudge`

To add a new category: add it to `test_cases.py` and to the `CATEGORIES` dict in `report.py`.

## Shipping thresholds

`sticky_message`, `call_dedup`, `language_pref`, `threep_nudge` must be **100%**. All others must be **≥80%** to ship.

## Key config values (`config.py`)

- `RESPONSE_TIMEOUT = 150` — seconds to wait for a single response
- `BURST_WAIT = 240` — seconds to wait for multiple responses
- `POLL_INTERVAL = 3` — how often chat.db is polled
- `GEMINI_MODEL = "models/gemini-3.1-flash-lite-preview"` — judge model

For slow call tests, override per-test with `"wait": 180` or more.

## Judge scoring

- **Code-based check first:** did the right number of responses arrive? Failures here override the LLM verdict.
- **LLM judge second:** Gemini scores content against `pass_criteria`. Write criteria as behavior, not wording — "did it make a call and return concrete info?" not "did it say 'I called on your behalf'".
- `PASS` verdicts can generally be trusted. Always investigate `FAIL` and `UNCLEAR` by reading the raw response in `results_*.json`.
