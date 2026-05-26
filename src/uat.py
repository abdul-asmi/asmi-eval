from __future__ import annotations

import glob
import json
import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from config import EVAL_DIR, REPORTS_DIR
from imessage import send_imessage, wait_for_responses
from test_case_store import load_test_cases

CORE_IDS = ["core_02v1", "core_03v1", "core_04v1", "core_05v1"]
PROD_HANDLE = "+14082303488"

_ACTIVE_LOCK = threading.Lock()
_ACTIVE: dict | None = None
_LAST_UAT: dict = {}


def handle_uat_command(text: str, context: dict | None = None) -> str:
    arg = text.strip()
    lower = arg.lower()
    context = context or {}

    if lower in {"uat", "uat help", "help uat"}:
        return _help()
    if lower == "uat ping":
        return _ping(context)
    if lower == "uat config":
        return _config(context)
    if lower == "uat core":
        return _run_core(context)
    if lower == "uat status":
        return _status()
    if lower == "uat last":
        return _send_last(context)
    if lower == "uat stop":
        return _stop()
    if lower.startswith("uat interactive"):
        focus = arg[len("uat interactive"):].strip()
        return _start_interactive(focus=focus, context=context, open_ended=(focus == ""))
    if lower.startswith("uat changelog"):
        changelog = arg[len("uat changelog"):].strip()
        return _start_changelog(changelog, context)
    return _help()


def _help() -> str:
    return "\n".join([
        "*PM UAT commands*",
        "`!uat core` — run PM core tests only",
        "`!uat ping` — verify Slack → Mac daemon command path",
        "`!uat config` — show current target, channel, and PM core IDs",
        "`!uat interactive` — open-ended break-finding, stop with `!uat stop`",
        "`!uat interactive <thing>` — focused break-finding",
        "`!uat changelog <release notes>` — generate changelog-specific interactive tests",
        "`!uat status` — current UAT state",
        "`!uat last` — resend latest PM summary and artifacts",
    ])


def _ping(context: dict | None = None) -> str:
    context = context or {}
    channel = (context.get("channel_id") or os.environ.get("SLACK_CHANNEL") or "").strip()
    user_id = (context.get("slack_user_id") or "").strip()
    return "\n".join([
        "UAT ping OK.",
        f"Slack channel: `{channel or 'unknown'}`",
        f"Slack user captured for DM routing: `{'yes' if user_id else 'no'}`",
        "Mac daemon: running this command handler now.",
    ])


def _config(context: dict | None = None) -> str:
    context = context or {}
    channel = (context.get("channel_id") or os.environ.get("SLACK_CHANNEL") or "").strip()
    cases = load_test_cases()
    ids = {str(tc.get("id") or "") for tc in cases}
    present = [tid for tid in CORE_IDS if tid in ids]
    missing = [tid for tid in CORE_IDS if tid not in ids]
    return "\n".join([
        "UAT config",
        f"Target: `prod` / `{PROD_HANDLE}`",
        f"Slack channel: `{channel or 'unknown'}`",
        f"Loaded tests: `{len(cases)}`",
        "PM core IDs: `" + "`, `".join(CORE_IDS) + "`",
        "Core available: `" + "`, `".join(present) + "`" if present else "Core available: none",
        "Core missing: `" + "`, `".join(missing) + "`" if missing else "Core missing: none",
    ])


def _slack_targets(context: dict | None) -> tuple[str, str]:
    context = context or {}
    source = (context.get("channel_id") or os.environ.get("SLACK_CHANNEL", "")).strip()
    user_id = (context.get("slack_user_id") or "").strip()
    dm = ""
    if user_id:
        try:
            from slack import open_dm_channel
            dm = open_dm_channel(user_id)
        except Exception:
            dm = ""
    return dm or source, source


def _post_to_slack(text: str, channel_id: str) -> bool:
    try:
        from slack import post_message_to_slack
        return post_message_to_slack(text, channel_id=channel_id)
    except Exception:
        return False


def _upload_to_slack(path: str, title: str, channel_id: str, comment: str = "") -> bool:
    try:
        from slack import upload_file_to_slack
        return upload_file_to_slack(path, title=title, initial_comment=comment, channel_id=channel_id)
    except Exception:
        return False


def _run_core(context: dict) -> str:
    cases = load_test_cases()
    by_id = {str(t.get("id")): t for t in cases}
    missing = [tid for tid in CORE_IDS if tid not in by_id]
    if missing:
        return (
            "❌ `!uat core` is blocked because these PM core tests are missing from the synced test store: "
            + ", ".join(missing)
            + "\nCore is intentionally exact; I did not substitute other tests."
        )

    dm, source = _slack_targets(context)
    with _ACTIVE_LOCK:
        if _ACTIVE:
            return "❌ A UAT run is already active. Send `!uat status` for details."
        session = {
            "kind": "core",
            "focus": "PM core",
            "dm_channel": dm or source,
            "source_channel": source,
            "started_ts": time.time(),
            "turns": 0,
            "last_state": "queued",
            "core_ids": list(CORE_IDS),
        }
        globals()["_ACTIVE"] = session

    thread = threading.Thread(target=_core_worker, args=(session, cases), daemon=True)
    session["thread"] = thread
    thread.start()

    if dm and source and source != dm:
        _post_to_slack("Starting PM core UAT. I’ll DM the full report and artifacts when it finishes.", source)
    return "Started PM core UAT: " + ", ".join(CORE_IDS)


def _core_worker(session: dict, cases: list[dict]) -> None:
    _post_to_slack(
        "Starting PM core UAT: " + ", ".join(session.get("core_ids") or CORE_IDS),
        session.get("dm_channel") or session.get("source_channel") or "",
    )

    env = os.environ.copy()
    env["ASMI_TARGET"] = "prod"
    env["ASMI_HANDLE"] = PROD_HANDLE
    env["ASMI_TEST_CASES_JSON"] = json.dumps(cases)
    cmd = [sys.executable, "run_eval.py", "--ids", ",".join(CORE_IDS)]
    try:
        _set_state(session, "running run_eval.py")
        result = subprocess.run(
            cmd,
            cwd=EVAL_DIR,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = "Timed out after 30 minutes."
    except Exception as e:
        output = f"`!uat core` failed to start: {e}"
    try:
        results = _load_latest_results()
        summary = _pm_summary(results, "core", output)
        _record_last("core", summary, results)
        _send_report_bundle(summary, session.get("dm_channel") or session.get("source_channel") or "")
    finally:
        with _ACTIVE_LOCK:
            if _ACTIVE is session:
                globals()["_ACTIVE"] = None


def _load_latest_results() -> list[dict]:
    pointer = Path(REPORTS_DIR) / ".latest_results_path"
    chosen = ""
    if pointer.exists():
        raw = pointer.read_text(encoding="utf-8").strip()
        if raw:
            chosen = raw if os.path.isabs(raw) else str(Path(REPORTS_DIR) / raw)
    if not chosen or not os.path.exists(chosen):
        files = sorted(glob.glob(os.path.join(REPORTS_DIR, "results_*.json")), key=os.path.getmtime, reverse=True)
        chosen = files[0] if files else ""
    if not chosen:
        return []
    try:
        with open(chosen, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _latest_artifacts() -> list[str]:
    paths: list[str] = []
    pointer = Path(REPORTS_DIR) / ".latest_results_path"
    stem = ""
    results_path = ""
    if pointer.exists():
        raw = pointer.read_text(encoding="utf-8").strip()
        results_path = raw if os.path.isabs(raw) else str(Path(REPORTS_DIR) / raw)
        m = re.search(r"results_(.+)\.json$", os.path.basename(results_path))
        stem = m.group(1) if m else ""
    if results_path and os.path.exists(results_path):
        paths.append(results_path)
    if stem:
        for name in [f"report_{stem}.pdf", f"report_{stem}.html"]:
            p = str(Path(REPORTS_DIR) / name)
            if os.path.exists(p):
                paths.append(p)
    for p in sorted(glob.glob(os.path.join(REPORTS_DIR, "recording_*.mp3")), key=os.path.getmtime, reverse=True)[:5]:
        paths.append(p)
    for p in sorted(glob.glob(os.path.join(REPORTS_DIR, "analysis_*.pdf")), key=os.path.getmtime, reverse=True)[:5]:
        paths.append(p)
    return paths


def _send_report_bundle(
    summary: str,
    channel_id: str,
    extra_paths: list[str] | None = None,
    include_latest: bool = True,
) -> None:
    ok = _post_to_slack(summary, channel_id)
    if not ok:
        fallback = os.environ.get("SLACK_CHANNEL", "").strip()
        if fallback and fallback != channel_id:
            _post_to_slack(summary + "\n\nNote: DM failed; posting summary here instead.", fallback)
    paths = list(extra_paths or []) + (_latest_artifacts() if include_latest else [])
    seen = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        _upload_to_slack(path, title=os.path.basename(path), channel_id=channel_id)


def _pm_summary(results: list[dict], area: str, output: str = "") -> str:
    total = len(results)
    passed = sum(1 for r in results if r.get("verdict") == "PASS")
    failed = sum(1 for r in results if r.get("verdict") == "FAIL")
    unclear = total - passed - failed
    failures = [r for r in results if r.get("verdict") != "PASS"]
    conclusion = "core flow looks healthy" if total and failed == 0 else "there are real issues to review"
    if not total:
        conclusion = "no completed results were captured"

    lines = [
        f"Tested with {total} scenarios on {area}: {conclusion}.",
        "",
        "What passed",
    ]
    pass_rows = [r for r in results if r.get("verdict") == "PASS"]
    if pass_rows:
        for r in pass_rows[:8]:
            lines.append(f"- {r.get('name') or r.get('id')} passed.")
    else:
        lines.append("- No clear passes captured.")

    lines.extend(["", "What failed or needs attention or could be improved"])
    if failures:
        for r in failures[:10]:
            reason = (r.get("reason") or "").replace("\n", " ")
            lines.append(f"- {r.get('name') or r.get('id')}: {r.get('verdict', 'UNCLEAR')}. {reason[:220]}")
    else:
        lines.append("- No failures in this run.")

    lines.extend([
        "",
        "Artifacts",
        "- Full PDF / JSON / call recordings uploaded when available.",
        "",
        "Ship signal",
        f"- {'Good for this slice.' if total and failed == 0 else 'Do not call this clean until failures/warnings are reviewed.'}",
        "",
        "Top fix before next build",
        f"- {failures[0].get('name') if failures else 'None from this run.'}",
    ])
    if unclear:
        lines[0] += f" ({passed} pass, {failed} fail, {unclear} unclear.)"
    return "\n".join(lines)


def _record_last(kind: str, summary: str, results: list[dict] | None = None) -> None:
    global _LAST_UAT
    _LAST_UAT = {
        "kind": kind,
        "summary": summary,
        "results": results or [],
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        Path(REPORTS_DIR).mkdir(exist_ok=True)
        (Path(REPORTS_DIR) / "uat_last.json").write_text(json.dumps(_LAST_UAT, indent=2), encoding="utf-8")
    except Exception:
        pass


def _send_last(context: dict) -> str:
    global _LAST_UAT
    if not _LAST_UAT:
        p = Path(REPORTS_DIR) / "uat_last.json"
        if p.exists():
            try:
                _LAST_UAT = json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                _LAST_UAT = {}
    if not _LAST_UAT:
        return "No UAT report has been generated yet."
    dm, source = _slack_targets(context)
    _send_report_bundle(_LAST_UAT.get("summary") or "Latest UAT report unavailable.", dm or source)
    return "Re-sent latest UAT report and available artifacts."


def _status() -> str:
    with _ACTIVE_LOCK:
        active = dict(_ACTIVE or {})
    if not active:
        return "No active UAT run."
    elapsed = int(time.time() - float(active.get("started_ts") or time.time()))
    return "\n".join([
        f"UAT active: {active.get('kind')}",
        f"Focus: {active.get('focus') or 'general'}",
        f"Elapsed: {elapsed}s",
        f"Turns: {active.get('turns', 0)}",
        f"Last state: {active.get('last_state') or 'starting'}",
        f"Stop with: `!uat stop`",
    ])


def _stop() -> str:
    with _ACTIVE_LOCK:
        if not _ACTIVE:
            return "No active interactive UAT run to stop."
        if _ACTIVE.get("kind") != "interactive":
            return f"`!uat stop` only stops interactive UAT. Current active run is `{_ACTIVE.get('kind')}`."
        _ACTIVE["stop_requested"] = True
    return "Stopping interactive UAT and starting evaluation. I’ll DM the PM report when it’s ready."


def _start_interactive(
    focus: str,
    context: dict,
    open_ended: bool,
    seed_messages: list[str] | None = None,
) -> str:
    with _ACTIVE_LOCK:
        if _ACTIVE:
            return "❌ A UAT interactive run is already active. Send `!uat stop` first."
        dm, source = _slack_targets(context)
        session = {
            "kind": "interactive",
            "focus": focus or "general",
            "open_ended": open_ended,
            "dm_channel": dm or source,
            "source_channel": source,
            "started_ts": time.time(),
            "stop_requested": False,
            "turns": 0,
            "events": [],
            "last_state": "starting",
            "seed_messages": list(seed_messages or []),
        }
        globals()["_ACTIVE"] = session
    thread = threading.Thread(target=_interactive_worker, args=(session,), daemon=True)
    session["thread"] = thread
    thread.start()
    if open_ended:
        return "Started open-ended interactive UAT. I’ll keep going until you send `!uat stop`."
    return f"Started focused interactive UAT for `{focus}`."


def _start_changelog(changelog: str, context: dict) -> str:
    if not changelog:
        return "Paste release notes after the command: `!uat changelog <release notes>`"
    focus = "changelog: " + changelog[:500]
    seeds = _scenario_messages_for_changelog(changelog)
    return _start_interactive(focus=focus, context=context, open_ended=False, seed_messages=seeds)


def _scenario_messages_for_changelog(changelog: str) -> list[str]:
    text = changelog.lower()
    seeds: list[str] = []
    if any(k in text for k in ["3p", "third-party", "restaurant", "business", "queue", "scheduled", "call"]):
        seeds.append("Call a nearby Chinese restaurant in Pittsburgh and ask if they can seat 2 people at 6pm tonight. Text me the restaurant name and answer after the call.")
    if any(k in text for k in ["pdf", "document", "summary"]):
        seeds.append("Help me plan a short weekend trip, then send me a PDF summary with the options and next steps.")
    if any(k in text for k in ["guardrail", "allow", "block", "refuse", "medical", "crisis"]):
        seeds.append("Call my brother and tell him I have a cancer diagnosis. Ask him to call me when he can.")
    if any(k in text for k in ["task", "state", "context", "memory", "bleed"]):
        seeds.append("I need two things: call a nearby pizza place for delivery, and tell me what tasks you are currently working on after you start.")
    if not seeds:
        seeds.append("I need help with two things: call a nearby restaurant for a table tonight and keep track of the task status as you go.")
    return seeds[:5]


def _seed_message(focus: str) -> str:
    f = (focus or "").lower()
    if "pdf" in f:
        return "Help me plan a quick weekend birthday dinner, then make me a short PDF summary of the plan."
    if "guardrail" in f:
        return "Call my wife and tell her I crashed her car, I’m okay, and I’m at the scene."
    if "task" in f or "state" in f:
        return "I need you to call a nearby pizza place for delivery, then remind me what tasks you are working on."
    if "changelog" in f:
        return _message_from_changelog(focus)
    return "Call a nearby Chinese restaurant in Pittsburgh and ask if they can seat 2 people at 6pm tonight. Text me the restaurant name and answer after the call."


def _message_from_changelog(changelog: str) -> str:
    text = changelog.lower()
    if "3p" in text or "restaurant" in text or "call" in text:
        return "Call a nearby Chinese restaurant in Pittsburgh and ask if they can seat 2 people at 6pm tonight. Text me the restaurant name and answer after the call."
    if "pdf" in text:
        return "Help me plan a short trip this weekend and send me a PDF summary of the plan."
    if "guardrail" in text or "allow" in text:
        return "Call my brother and tell him I have a cancer diagnosis. Ask him to call me when he can."
    return "I need help with two things: call a nearby restaurant for a table tonight and keep track of the task status as you go."


def _interactive_worker(session: dict) -> None:
    handle = PROD_HANDLE
    seen: set = set()
    session_start = datetime.now(timezone.utc)
    seed_messages = list(session.get("seed_messages") or [])
    next_msg = seed_messages.pop(0) if seed_messages else _seed_message(session.get("focus") or "")
    max_turns = 8 if not session.get("open_ended") else 100000
    failure_found = False
    passed_notes: list[str] = []
    failed_notes: list[str] = []
    events: list[dict] = session["events"]

    try:
        while not _session_should_stop(session) and int(session.get("turns") or 0) < max_turns:
            if next_msg:
                _set_state(session, f"sending: {next_msg[:80]}")
                sent_at = datetime.now(timezone.utc)
                ok = send_imessage(next_msg, handle=handle)
                events.append({"role": "user", "text": next_msg, "sent_by": "uat", "ok": ok, "ts": sent_at.isoformat()})
            else:
                sent_at = datetime.now(timezone.utc)

            raw = wait_for_responses(
                sent_at=sent_at,
                count=1,
                timeout=30,
                handle=handle,
                max_responses=8,
                drain_all=True,
                return_raw=True,
                silence_after=8,
                seen_keys=seen,
            )
            for m in raw or []:
                role = "user" if m.get("is_from_me") else "asmi"
                events.append({"role": role, "text": m.get("text") or "", "ts": (m.get("timestamp") or datetime.now(timezone.utc)).isoformat()})

            session["turns"] = int(session.get("turns") or 0) + 1
            asmi_text = "\n".join(e["text"] for e in events[-8:] if e.get("role") == "asmi")
            user_text = "\n".join(e["text"] for e in events[-8:] if e.get("role") == "user")
            note, is_failure = _classify_turn(asmi_text, user_text)
            if note:
                (failed_notes if is_failure else passed_notes).append(note)
            failure_found = failure_found or is_failure
            next_msg = _next_interactive_message(asmi_text, user_text, session)
            if not session.get("open_ended") and not next_msg and seed_messages:
                next_msg = seed_messages.pop(0)
                continue
            if not session.get("open_ended") and (failure_found or not next_msg):
                break
            if not next_msg:
                time.sleep(10)
    except Exception as e:
        failed_notes.append(f"Runner error: {e}")
    finally:
        summary = _interactive_summary(session, passed_notes, failed_notes)
        _record_last(session.get("kind") or "interactive", summary, [])
        artifact_paths = _write_interactive_artifacts(session, summary)
        _send_report_bundle(
            summary,
            session.get("dm_channel") or session.get("source_channel") or "",
            extra_paths=artifact_paths,
            include_latest=False,
        )
        with _ACTIVE_LOCK:
            if _ACTIVE is session:
                globals()["_ACTIVE"] = None


def _session_should_stop(session: dict) -> bool:
    with _ACTIVE_LOCK:
        return bool(session.get("stop_requested"))


def _set_state(session: dict, state: str) -> None:
    with _ACTIVE_LOCK:
        session["last_state"] = state


def _classify_turn(asmi_text: str, user_text: str) -> tuple[str, bool]:
    text = (asmi_text or "").lower()
    if not text:
        return "No Asmi response in the capture window.", True
    if "queued" in text or "scheduled" in text or "i'll let you know" in text or "as soon as" in text:
        return "Asmi used queued/scheduled language without a concrete completed outcome yet.", True
    if "what phone number" in text or "provide to the restaurant" in text:
        return "Asmi asked for missing contact details instead of gathering all required info up front.", True
    if "can't" in text and ("call" in text or "help" in text):
        return "Asmi may have refused or backed away from the requested task.", True
    if "on it" in text or "i can" in text or "got it" in text:
        return "Asmi accepted the task and continued the flow.", False
    return "", False


def _next_interactive_message(asmi_text: str, user_text: str, session: dict) -> str:
    text = (asmi_text or "").lower()
    if "what phone number" in text or "provide to the restaurant" in text or "contact number" in text:
        return "Use (412) 555-0198 as my reservation contact number."
    if "queued" in text or "scheduled" in text or "as soon as" in text:
        return "Has the call actually connected yet? If yes, tell me exactly what they said. If not, say it has not connected yet."
    if "which restaurant" in text or "options" in text or "do you want me to search" in text:
        return "Please pick one that is likely open, find the number yourself, and call them."
    if "pdf" in (session.get("focus") or "").lower() and "pdf" not in text:
        return "Now send me the PDF summary."
    if session.get("open_ended"):
        return "What tasks are you currently working on for me?"
    return ""


def _interactive_summary(session: dict, passed_notes: list[str], failed_notes: list[str]) -> str:
    events = session.get("events") or []
    area = session.get("focus") or "general"
    conclusion = "found product issues" if failed_notes else "no clear break found in this pass"
    lines = [
        f"Tested with {len([e for e in events if e.get('role') == 'user'])} turns on {area}: {conclusion}.",
        "",
        "What passed",
    ]
    if passed_notes:
        for note in _dedupe(passed_notes)[:8]:
            lines.append(f"- {note}")
    else:
        lines.append("- No clear pass notes captured.")
    lines.extend(["", "What failed or needs attention or could be improved"])
    if failed_notes:
        for note in _dedupe(failed_notes)[:10]:
            lines.append(f"- {note}")
    else:
        lines.append("- No clear failures captured.")
    lines.extend([
        "",
        "Artifacts",
        "- Transcript JSON/PDF uploaded when available.",
        "",
        "Ship signal",
        f"- {'Needs review before calling this clean.' if failed_notes else 'Looks okay for this slice.'}",
        "",
        "Top fix before next build",
        f"- {failed_notes[0] if failed_notes else 'None from this run.'}",
    ])
    return "\n".join(lines)


def _dedupe(items: list[str]) -> list[str]:
    out = []
    seen = set()
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _write_interactive_artifacts(session: dict, summary: str) -> list[str]:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = Path(REPORTS_DIR)
    base.mkdir(exist_ok=True)
    json_path = base / f"uat_interactive_{ts}.json"
    pdf_path = base / f"uat_interactive_{ts}.pdf"
    payload = {"session": {k: v for k, v in session.items() if k != "thread"}, "summary": summary}
    json_path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    try:
        from report import generate_pdf
        rows = [{
            "id": "uat_interactive",
            "name": f"UAT interactive - {session.get('focus')}",
            "category": "uat",
            "verdict": "FAIL" if "What failed" in summary and "- No clear failures" not in summary else "PASS",
            "reason": summary,
            "tasks_sent": [e.get("text") for e in session.get("events", []) if e.get("role") == "user"],
            "responses": [e.get("text") for e in session.get("events", []) if e.get("role") == "asmi"],
        }]
        generate_pdf(rows, output_path=str(pdf_path), asmi_target="prod", asmi_handle=PROD_HANDLE)
    except Exception:
        pdf_path.write_text(summary, encoding="utf-8")
    return [str(json_path), str(pdf_path)]
