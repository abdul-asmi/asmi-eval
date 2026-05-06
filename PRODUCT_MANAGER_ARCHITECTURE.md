# Asmi Eval System: Product Manager Version

This doc explains what you built in plain English. It is separate from `SYSTEM_ARCHITECTURE.md`, which is more technical.

Keep this file updated whenever the product behavior changes.

## What This Product Is

You built a testing dashboard for Asmi, your iMessage-based AI assistant.

The system lets you:

- write test scenarios for Asmi
- send those scenarios to Asmi through real iMessage
- collect Asmi's real replies
- judge whether each reply passed or failed
- review run history
- inspect the exact messages and responses
- generate reports

In simple terms:

> This is a QA cockpit for testing whether Asmi behaves correctly in real conversations.

## Why It Exists

Asmi can fail in subtle ways:

- not replying
- replying too late
- forgetting context
- asking unnecessary follow-up questions
- making duplicate calls
- giving vague summaries
- using the wrong timezone
- failing to complete a task

Manual testing is slow and inconsistent. This system makes the same checks repeatable.

## The Product Surfaces

### 1. Tests Tab

This is the main test library.

You can:

- search tests
- filter by category/type
- expand categories
- select tests with checkboxes
- run selected tests
- edit individual test details
- add or save tests

Important UI rule:

- Category expand shows compact rows only.
- Full details open only when you click a specific test row.
- There is only one select-all checkbox: the table header checkbox in the ID row.

### 2. History Tab

This shows past runs.

Each row represents one eval run and shows:

- run timestamp in Eastern time
- total tests
- passed count
- failed count
- unclear count
- pass percentage
- report actions

The History tab reads from saved run result files in `reports/`.

### 3. Responses Tab

This shows the actual conversation evidence.

For each run, it shows:

- messages sent to Asmi
- responses received from Asmi
- response count

This is where you inspect what Asmi actually said, not just the pass/fail score.

When a run finishes, the UI also writes the newest report artifacts into `reports/` on the server side, so the Railway site can show fresh data right after refresh. If GitHub sync is configured, those same files are pushed back to the repo too.

## The Big Flow

Here is what happens when you click Run selected:

```text
You select tests in the browser
        |
        v
UI queues the run
        |
        v
Background daemon picks up the run
        |
        v
Eval runner sends real iMessages to Asmi
        |
        v
Asmi replies in iMessage
        |
        v
System reads replies from macOS Messages database
        |
        v
Gemini judges whether each test passed
        |
        v
System saves results and report files
        |
        v
UI updates live output, History, and Responses, then persists the latest run for Railway refreshes
```

## APIs Involved

There are two kinds of APIs in this system:

1. Internal local APIs: your browser, UI server, and daemon use these to talk to each other.
2. External service APIs: the system uses these to talk to Gemini, GitHub, Apple Messages, and Asmi.

### Internal Local APIs

These run inside `ui.py` at:

```text
http://localhost:8765
```

You usually do not call these by hand. The browser UI and daemon call them.

| API | Who Calls It | What It Does |
|---|---|---|
| `GET /api/tests` | Browser | Loads all test cases into the Tests tab. |
| `POST /api/tests` | Browser | Saves edited tests back to `test_cases.py` or GitHub, depending on config. |
| `POST /api/run` | Browser | Queues a run when you click Run selected. |
| `GET /api/poll` | Daemon | Daemon asks the UI, "Is there a run waiting?" |
| `POST /api/output` | Daemon | Daemon posts final run output and judged results back to the UI. |
| `GET /api/output` | Browser | Browser polls this to update live run status and inline results. |
| `POST /api/progress` | Daemon | Daemon posts the current test/progress count while a run is active. |
| `GET /api/progress` | Browser | Browser can read current run progress. |
| `GET /api/history` | Browser | Loads the History tab from saved `reports/results_*.json` files plus latest in-memory run. |
| `GET /api/responses` | Browser | Loads the Responses tab with messages sent and Asmi replies. |
| `GET /api/report/<run_id>` | Browser | Opens a saved HTML report in the same tab. |
| `POST /api/stop` | Browser | Sends a stop signal for the daemon to halt after the current test. |
| `POST /api/generate` | Browser | Uses Gemini to generate draft test cases from a prompt. |
| `POST /api/analyze` | Browser | Uses Gemini to summarize behavior patterns across a run. |
| `POST /api/save-behavior` | Browser | Saves generated behavior analysis into `ASMI_BEHAVIOR_ANALYSIS.md`. |
| `GET /health` | Browser/Debug | Confirms the UI server is alive. |

### Where These APIs Live In Python Files

If you want to inspect the real implementation, this is the exact map:

| Python File | Where | What It Contains |
|---|---|---|
| `ui.py` | `class Handler(BaseHTTPRequestHandler)` -> `do_GET` and `do_POST` | All local API endpoints (`/api/tests`, `/api/run`, `/api/poll`, `/api/output`, `/api/history`, `/api/responses`, etc.). |
| `ui.py` | Frontend JS inside `HTML = """..."""` | Browser `fetch('/api/...')` calls from Tests/History/Responses tabs. |
| `daemon.py` | `_poll_railway_full()` | Daemon polling logic for `/api/poll` (Railway first, then local UI fallback). |
| `daemon.py` | `_post_progress()` | Daemon progress updates to `/api/progress`. |
| `daemon.py` | `_post_output_to_railway()` and `_post_output_to_local_ui()` | Daemon final run results posted to `/api/output`. |
| `config.py` | `RAILWAY_URL`, `LOCAL_UI_URL` | Chooses whether daemon talks to Railway or local UI. |

### What Happens During A UI Run

The key internal API chain is:

```text
Browser -> POST /api/run
Daemon  -> GET /api/poll
Daemon  -> runs run_eval.py
Daemon  -> POST /api/output
Browser -> GET /api/output
Browser -> GET /api/history
Browser -> GET /api/responses
```

If History or Responses look stale, these are the APIs to check:

```text
GET /api/history
GET /api/responses
GET /api/output
```

### External APIs And Systems

| System | Used By | What It Does |
|---|---|---|
| Apple Messages / iMessage | `imessage.py` | Sends real messages to Asmi and reads replies from `chat.db`. |
| macOS `chat.db` | `imessage.py`, `daemon.py` | Local Messages database used to collect Asmi replies and command messages. |
| Gemini API | `judge.py`, `ui.py`, `fix_gemini.py` | Judges pass/fail, generates test cases, and writes behavior analysis. |
| GitHub Contents API | `ui.py` when GitHub env vars are configured | Reads/writes `test_cases.py` remotely instead of local disk. |
| Asmi iMessage service | Real external product under test | Receives the test messages and replies like a user-facing assistant. |
| Railway URL, optional | `daemon.py` | Remote hosted UI mode. Currently local development uses `LOCAL_UI_URL`. |

## Full System Flowchart (Local + Railway)

This is the complete architecture flow including both deployment paths.

```text
                           ┌─────────────────────────────┐
                           │        Browser UI           │
                           │   (Tests/History/Responses) │
                           └──────────────┬──────────────┘
                                          │
                                          │ fetch /api/*
                                          v
                      ┌────────────────────────────────────────┐
                      │   UI Server (ui.py, localhost:8765)   │
                      │   Handler.do_GET / Handler.do_POST     │
                      └─────────────────┬──────────────────────┘
                                        │
                     queues run         │ GET /api/poll
                    POST /api/run       │ (heartbeat + run pickup)
                                        v
                         ┌────────────────────────────────┐
                         │      Daemon (daemon.py)        │
                         │  _poll_railway_full()          │
                         └──────────────┬─────────────────┘
                                        │
                                        │ runs subprocess
                                        v
                           ┌────────────────────────────┐
                           │   run_eval.py / runner.py  │
                           └──────────────┬─────────────┘
                                          │
                  sends iMessage          │ reads replies
                                          v
                         ┌────────────────────────────────┐
                         │ Apple Messages + chat.db       │
                         │ (real user conversation path)  │
                         └────────────────┬───────────────┘
                                          │
                                          │ judge call
                                          v
                               ┌──────────────────────┐
                               │  Gemini API (judge)  │
                               └──────────┬───────────┘
                                          │
                                          │ writes artifacts
                                          v
                          ┌──────────────────────────────────┐
                          │ reports/results_*.json           │
                          │ reports/report_*.html            │
                          └────────────────┬─────────────────┘
                                           │
                                           │ POST /api/output
                                           v
                      ┌────────────────────────────────────────┐
                      │ UI Server state (_run_output/results)  │
                      └─────────────────┬──────────────────────┘
                                        │
                     Browser polls      │ Browser loads tabs
                     GET /api/output    │ GET /api/history,/responses
                                        v
                           ┌────────────────────────────┐
                           │ Updated Output/History/Resp│
                           └────────────────────────────┘
```

### Railway Connection Path

When hosted, the daemon prefers Railway:

```text
daemon.py:
  if RAILWAY_URL is set:
    poll  -> {RAILWAY_URL}/api/poll
    post  -> {RAILWAY_URL}/api/progress
    post  -> {RAILWAY_URL}/api/output

  fallback:
    if Railway fails (or RAILWAY_URL is None), use LOCAL_UI_URL
```

So you effectively have two targets for the same API contract:

1. Remote: Railway UI server
2. Local: `http://127.0.0.1:8765`

### API Mental Model

The browser UI is not the test runner. It is the control panel.

The daemon is the worker.

The APIs are the handoff points:

- Browser says: "Please run these tests."
- Daemon says: "I picked up the job."
- Runner says: "I sent messages and got responses."
- Judge says: "These passed/failed."
- Daemon says: "Here are the finished results."
- Browser says: "Show them in Output, History, and Responses."

## What The Important Files Mean

| File | Product Meaning |
|---|---|
| `ui.py` | The browser dashboard. This is what you see at `localhost:8765`. |
| `test_cases.py` | The source of truth for all tests. Every row in the Tests tab comes from here. |
| `run_eval.py` | Starts a test run from the command line or daemon. |
| `runner.py` | Handles the actual test flow: send message, wait, collect responses. |
| `imessage.py` | Talks to Apple Messages. Sends iMessages and reads replies. |
| `judge.py` | Asks Gemini to decide pass/fail. |
| `report.py` | Builds the HTML report. |
| `daemon.py` | Background worker that lets the browser queue runs. |
| `reports/` | Folder where past run results and reports are saved. |
| `SYSTEM_ARCHITECTURE.md` | More technical architecture map for engineers/agents. |

## What A Test Case Is

A test case is one scenario you want Asmi to handle.

Example:

```text
User asks: "Call a dentist and ask their next cleaning availability."

Pass if:
Asmi actually attempts the call and returns a specific result.

Fail if:
Asmi only gives a phone number, asks unnecessary questions, or never reports back.
```

Each test has:

- ID
- name
- category
- type
- message or messages
- wait time
- pass criteria

## Test Types In Plain English

| Type | Meaning |
|---|---|
| `single` | Send one message and wait for a reply. |
| `sequence` | Send multiple messages step by step. |
| `burst` | Send several messages quickly, like a stressed real user. |
| `dedup` | Send duplicate/similar requests and make sure Asmi does not double-act. |
| `burst_with_setup` | Set context first, then send a burst of tasks. |

## Current Categories

Categories are buckets of behavior.

Examples:

- onboarding
- capability
- sticky_message
- call_dedup
- call_summary
- voicemail
- task_reliability
- threep_nudge
- location_memory
- language_pref
- timezone
- checklist
- chat_brevity
- chat_flow
- personalization
- reengagement
- guardrails

## Where Results Live

Every run creates files in `reports/`.

Example:

```text
reports/results_20260506_001311.json
reports/report_20260506_001311.html
```

The timestamp means:

```text
2026-05-06 at 00:13:11 Eastern time
```

The UI should show this as:

```text
05/06/2026 00:13:11 ET
```

## What "History Not Updating" Means

If a run finishes but you do not see it:

1. Check whether a new `reports/results_*.json` file exists.
2. Check whether the browser UI has refreshed.
3. Check whether the daemon posted results back to the UI.

The UI now auto-refreshes History and Responses while those tabs are open.

## What To Remember When Iterating

Product rules we care about:

- The UI should feel like a working test dashboard, not a landing page.
- Selection should be obvious and not duplicated.
- Test rows should stay compact unless the user explicitly opens details.
- History should show correct Eastern wall time.
- Responses should show the actual evidence.
- Reports should open without popups.
- The dashboard should keep running in the background on `localhost:8765`.

## If You Ask Codex To Change This Later

Ask for changes in product language, like:

- "Make History update right after a run finishes."
- "Show only compact rows when expanding categories."
- "Add a better way to select all visible tests."
- "Explain why a test failed in the Responses tab."

Then Codex should update:

- the code
- `SYSTEM_ARCHITECTURE.md` if the technical behavior changed
- this file if the product behavior changed

## Current State As Of 2026-05-06

- The UI runs locally at `http://localhost:8765`.
- The main dashboard is `ui.py`.
- History and Responses read from `reports/`.
- History timestamps are Eastern wall time from filenames.
- Select-all exists only in the table header checkbox.
- Category expansion shows compact rows only.
- Full details open only by clicking a test row.
- History/Responses auto-refresh while visible.
