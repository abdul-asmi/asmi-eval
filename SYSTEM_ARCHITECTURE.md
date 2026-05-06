# Asmi Eval System Architecture

This is the working map for you and for future coding agents. Keep this file updated whenever the system behavior, run flow, UI rules, or operational commands change.

## Purpose

This repo tests Asmi through the real iMessage interface. It sends messages to Asmi, reads replies from macOS Messages storage, judges the replies with Gemini, and saves run artifacts for review.

## Mental Model

Think of the system as five layers:

1. Test definitions: `test_cases.py`
2. Execution engine: `run_eval.py` -> `runner.py` -> `imessage.py`
3. Judge/reporting: `judge.py`, `rejudge.py`, `report.py`
4. Local web control panel: `ui.py`
5. Automation bridge: `daemon.py`

The UI does not run tests directly. It queues a run. The daemon picks up that queue item, runs the CLI, then posts results back to the UI.

## Main Files

| File | Role |
|---|---|
| `ui.py` | Local browser UI at `http://localhost:8765`. Edits tests, queues runs, displays live output, History, and Responses. The HTML/JS is embedded in a Python string, so the server must restart after UI edits. |
| `daemon.py` | Background runner bridge. Polls the UI for queued runs, runs `run_eval.py`, posts output/results back to the UI, and can also listen for iMessage commands. |
| `run_eval.py` | CLI entry point. Parses filters, calls `runner.run_all`, writes `reports/results_*.json`, and generates `reports/report_*.html`. |
| `runner.py` | Executes test cases by type: `single`, `burst`, `sequence`, `dedup`, `burst_with_setup`. Sends messages, waits, gathers responses, calls the judge. |
| `imessage.py` | Sends iMessages and reads `~/Library/Messages/chat.db`. Requires Full Disk Access. |
| `judge.py` | Gemini judge for standard runs. |
| `rejudge.py` | Re-scores an existing results JSON without resending iMessages. |
| `report.py` | Generates the HTML report from result dictionaries. |
| `test_cases.py` | Canonical test definitions. |
| `config.py` | Local config: handles, timeouts, model, report directory, local UI URL. Treat as private. |

## Run Lifecycle

### From The Browser UI

1. User selects tests in `ui.py` and clicks Run selected.
2. `POST /api/run` stores `_pending_run` in the UI process.
3. `daemon.py` polls `GET /api/poll` and receives the pending run.
4. Daemon launches `run_eval.py` with the right filter.
5. `run_eval.py` calls `runner.py`.
6. `runner.py` sends messages via `imessage.py`, waits for replies, and judges with `judge.py`.
7. `run_eval.py` writes:
   - `reports/results_YYYYMMDD_HHMMSS.json`
   - `reports/report_YYYYMMDD_HHMMSS.html`
8. `daemon.py` posts final output and parsed results to `POST /api/output`.
9. `ui.py` persists the latest run back into `reports/` on the server side, and then exposes it through `/api/output`, `/api/history`, and `/api/responses`.

### From Terminal

When you run `python3 run_eval.py --id voicemail_01`, the UI is not involved. Results still get written to `reports/`, but live output does not appear in the browser unless the daemon/UI post path is used.

### From iMessage Command Daemon

The daemon can also listen for command messages such as `!run voicemail_01`. In that path, `daemon.py` calls the same `run_eval.py` CLI and can post results to the local UI if `LOCAL_UI_URL` points at `http://127.0.0.1:8765`.

## Data Flow Diagram

```text
Browser UI
  |
  | POST /api/run
  v
ui.py in-memory pending run
  ^
  | GET /api/poll
  |
daemon.py
  |
  | subprocess
  v
run_eval.py
  |
  v
runner.py
  |
  +--> imessage.py -> macOS Messages / chat.db
  |
  +--> judge.py -> Gemini
  |
  v
reports/results_*.json + reports/report_*.html
  |
  | POST /api/output
  v
ui.py output/history/responses APIs
```

## UI State Rules

- Category expand/collapse should show compact rows only.
- Full task details should open only when the user clicks an individual test row or edit button.
- Header select-all and toolbar select-all are checkboxes, not text buttons.
- Select-all acts on the currently visible filtered rows.
- Category checkboxes should be checked when all visible tests in that category are selected and indeterminate when partially selected.
- History report actions should not use popups/new tabs. Use same-tab navigation/download.
- History and Responses should refresh after a run completes if either tab is open.
- History and Responses should also auto-refresh every 5 seconds while visible, because runs can complete outside the current tab render path.
- `ui.py` embeds HTML at startup. Restart the server after changing UI code.

## History and Responses Sync

History and Responses are sourced from two places:

1. Persisted files in `reports/results_*.json`.
2. The most recent in-memory run posted to `POST /api/output`.

Why both matter:

- File scanning is durable and survives server restart.
- In-memory latest run makes the newest result visible immediately when the daemon posts output.

Expected behavior:

- After a UI-triggered run finishes, the open History or Responses tab refreshes automatically.
- If you are already on History/Responses, it refreshes every 5 seconds.
- If you refresh the browser, the UI reloads from `reports/`.

Quick debug:

```bash
ls -lt reports/results_*.json | head
python3 - <<'PY'
import json, urllib.request
for path in ("history", "responses"):
    data = json.loads(urllib.request.urlopen(f"http://localhost:8765/api/{path}").read())
    print(path, data[0]["stem"] if data else None)
PY
```

## Local Server Notes

Start in the foreground for debugging:

```bash
python3 ui.py
```

Start detached in the background:

```bash
python3 -c 'import os,sys; cwd="/Users/yaybeedee/Desktop/asmi/eval"; log=os.path.join(cwd,"ui.log"); pid=os.fork();
if pid: sys.exit(0)
os.setsid(); pid=os.fork();
if pid: sys.exit(0)
os.chdir(cwd); os.environ["PORT"]="8765"; fd=os.open("/dev/null", os.O_RDONLY); os.dup2(fd,0); os.close(fd); fd=os.open(log, os.O_WRONLY|os.O_CREAT|os.O_APPEND, 0o644); os.dup2(fd,1); os.dup2(fd,2); os.close(fd); os.execvp("python3", ["python3", "-u", "ui.py"])'
```

Verify:

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8765/
lsof -nP -iTCP:8765 -sTCP:LISTEN
```

Stop:

```bash
kill $(lsof -tiTCP:8765 -sTCP:LISTEN)
```

## Report and History Data

Primary persisted artifacts live in `reports/`:

- `results_*.json`: machine-readable result list
- `report_*.html`: human report
- `.latest_results_path`: portable latest-run pointer used by the daemon when available
- `overall_analysis.json`: cumulative analysis summary

The UI now writes completed runs into `reports/` when `/api/output` arrives, so the Railway deployment can show fresh results on refresh without waiting for a redeploy. When GitHub is configured, the same artifacts are synced back to the repo too.

The UI also keeps the latest posted run in memory so History/Responses can show it immediately even if file scanning is slightly behind.

## Existing Documentation

- `README.md`: setup and operator guide.
- `PRODUCT_MANAGER_ARCHITECTURE.md`: plain-English product map for non-engineers.
- `CLAUDE.md`: coding-agent quick context.
- `EVAL_GUIDE.md`: living eval process and testing playbook.
- `TEST_CASE_GUIDE.md`: test schema and test-writing guide.
- `MECE_COVERAGE_FRAMEWORK.md`: coverage design framework.
- `ASMI_BEHAVIOR_ANALYSIS.md`: accumulated behavior observations.
- `tasks-for-me.md`: local task notes.

## Documentation Update Rule

When changing the system, update this file if the change affects:

- how runs are queued, executed, judged, or reported
- how the UI behaves
- how History/Responses are populated
- how to start, stop, or verify servers/daemons
- where data is stored
- what future agents should preserve

Small test wording changes usually belong in `test_cases.py` and `EVAL_GUIDE.md`, not here.

## Common Failure Modes

- `localhost:8765` serves old HTML: restart `ui.py`.
- Browser cannot reach `localhost:8765`: server is stopped or bound to a different port.
- History shows stale data: check latest `reports/results_*.json`, then confirm daemon posted to `/api/output`.
- Latest run exists in `reports/` but not in browser: refresh the page, or check that served HTML includes `_startHistoryAutoRefresh`.
- `Invalid Date` in History: filename timestamp format is not being parsed.
- No iMessage responses: check Full Disk Access and `ASMI_HANDLE`.

## Verification Checklist After UI Changes

```bash
python3 -m py_compile ui.py
curl -s http://localhost:8765/ | rg "_refreshHistoryTabs|_startHistoryAutoRefresh|location.href='/api/report"
curl -s http://localhost:8765/api/tests | head -c 80
```

Manual UI checks:

- Search/filter changes update visible rows.
- Top checkbox selects visible tests only.
- Category checkbox selects that category.
- Partial category selection shows an indeterminate checkbox.
- Run completion updates History/Responses.

## Change Log

- 2026-05-06: Added compact category expand rule, no-popup History actions, robust History timestamp parsing, latest-run fallback in History/Responses, visible-tab auto-refresh, checkbox-only toolbar select-all, and this architecture document.
- 2026-05-06: Corrected History/Responses timestamps to display report filename time as Eastern wall time, not as UTC converted to ET. Added `PRODUCT_MANAGER_ARCHITECTURE.md`.
- 2026-05-06: Added plain-English API inventory to `PRODUCT_MANAGER_ARCHITECTURE.md`.
- 2026-05-06: Added API location map (which Python files/functions own each endpoint) and full local+Railway flowchart to `PRODUCT_MANAGER_ARCHITECTURE.md`.
