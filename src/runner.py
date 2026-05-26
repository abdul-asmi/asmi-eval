import time
import asyncio
import threading
from datetime import datetime, timezone
import os
import json
import hashlib
import hmac
import urllib.parse

from config import RESPONSE_TIMEOUT, BURST_WAIT, BURST_SEND_DELAY, SEQUENCE_DELAY, SILENCE_AFTER, CMD_ONBOARD, CATEGORY_RUN_ORDER, JUDGE_DELAY, CALL_EVAL_PHONE, CALL_TRANSCRIPT_TIMEOUT, CALL_EVAL_MAX_DURATION, COMMAND_HANDLE, RAILWAY_URL, DAEMON_TOKEN, WHATSAPP_PROD_HANDLE, WHATSAPP_DEV_HANDLE
from imessage import send_imessage, wait_for_responses, catch_up_manual_messages
from whatsapp_channel import send_whatsapp, wait_for_whatsapp_responses, catch_up_whatsapp_messages, ensure_whatsapp_session
from judge import judge_status, judge_with_context, judge_response_count
from elevenlabs_phone import list_recent_conversations, wait_for_call_transcript
from twilio_phone import twilio_configured, newest_active_call_sid, end_call

try:
    import websockets
    _WEBSOCKETS_AVAILABLE = True
except ImportError:
    websockets = None
    _WEBSOCKETS_AVAILABLE = False


def _stop_requested() -> bool:
    path = os.environ.get("ASMI_STOP_FILE", "").strip()
    return bool(path and os.path.exists(path))


def _sleep_interruptible(seconds: float) -> bool:
    """Sleep in short chunks. Returns False if a stop was requested."""
    deadline = time.time() + max(0.0, float(seconds or 0))
    while time.time() < deadline:
        if _stop_requested():
            print("\n  ⏹ Stop requested — not sending more messages")
            return False
        time.sleep(min(0.5, deadline - time.time()))
    return not _stop_requested()


def _next_message_delay(msg: str, default_delay: float, command_delay: float = 10.0) -> float:
    """
    Shorten the pause after command-style messages so the next task follows quickly.
    """
    text = (msg or "").strip()
    if text.startswith("cmd_"):
        return float(command_delay)
    return float(default_delay or 0.0)


def _monitor_event_summary(event: dict) -> str:
    etype = str(event.get("type") or "event").strip()
    if etype == "user_transcript":
        payload = event.get("user_transcription_event") or {}
        text = str(payload.get("user_transcript") or "").strip()
        return f"user_transcript: {text}" if text else "user_transcript"
    if etype == "agent_response":
        payload = event.get("agent_response_event") or {}
        text = str(payload.get("agent_response") or "").strip()
        return f"agent_response: {text}" if text else "agent_response"
    if etype == "agent_chat_response_part":
        payload = event.get("text_response_part") or {}
        part = str(payload.get("type") or "").strip()
        text = str(payload.get("text") or "").strip()
        if part == "delta" and text:
            return f"agent_chat_response_part: {text}"
        return f"agent_chat_response_part({part})" if part else "agent_chat_response_part"
    if etype == "agent_tool_response":
        payload = event.get("agent_tool_response") or {}
        tool = str(payload.get("tool_name") or "tool").strip()
        if payload.get("is_error"):
            return f"agent_tool_response: {tool} (error)"
        return f"agent_tool_response: {tool}"
    if etype == "agent_response_complete":
        return "agent_response_complete"
    return etype


def _start_elevenlabs_monitor(agent_id: str, started_after: datetime, events: list[dict], state: dict, stop_event: threading.Event) -> threading.Thread | None:
    if not _WEBSOCKETS_AVAILABLE:
        state["status"] = "disabled"
        state["error"] = "websockets package is missing (install requirements.txt)"
        return None
    if not agent_id or not started_after:
        state["status"] = "disabled"
        state["error"] = "ELEVENLABS_AGENT_ID or call start timestamp missing"
        return None

    api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not api_key:
        state["status"] = "disabled"
        state["error"] = "ELEVENLABS_API_KEY is missing"
        return None

    started_ts = started_after.replace(tzinfo=timezone.utc) if started_after.tzinfo is None else started_after.astimezone(timezone.utc)

    async def _monitor_ws(conversation_id: str):
        url = f"wss://api.elevenlabs.io/v1/convai/conversations/{conversation_id}/monitor"
        headers = [("xi-api-key", api_key)]
        async with websockets.connect(url, additional_headers=headers, open_timeout=10) as ws:
            state["connected"] = True
            state["conversation_id"] = conversation_id
            state["status"] = "monitoring"
            while not stop_event.is_set():
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                except Exception as e:
                    state["error"] = str(e)
                    break

                ts = datetime.now(timezone.utc).isoformat()
                try:
                    evt = json.loads(msg) if isinstance(msg, str) else {"type": "raw", "raw": msg}
                except Exception:
                    evt = {"type": "raw", "raw": msg}
                events.append({
                    "timestamp": ts,
                    "type": evt.get("type") or "raw",
                    "summary": _monitor_event_summary(evt),
                    "raw": evt,
                })
                print(f"  [ElevenLabs live] {events[-1]['summary']}")

    def _runner():
        deadline = time.time() + 300
        conversation_id = None
        while time.time() < deadline and not stop_event.is_set():
            try:
                convos = list_recent_conversations(agent_id)
            except Exception as e:
                state["error"] = f"list_recent_conversations: {e}"
                time.sleep(1)
                continue

            for convo in convos:
                raw_start = (
                    convo.get("start_time_unix_secs")
                    or convo.get("started_at_unix_secs")
                    or convo.get("created_at_unix_secs")
                    or 0
                )
                if raw_start:
                    convo_start = datetime.fromtimestamp(raw_start, tz=timezone.utc)
                else:
                    iso = convo.get("start_time") or convo.get("started_at") or ""
                    if not iso:
                        continue
                    try:
                        convo_start = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    except Exception:
                        continue
                if convo_start < started_ts:
                    continue
                conversation_id = convo.get("conversation_id") or convo.get("id")
                if not conversation_id:
                    continue
                state["conversation_id"] = conversation_id
                state["conversation_status"] = str(convo.get("status") or "").lower()
                print(f"  [ElevenLabs] conversation_id: {conversation_id}", flush=True)
                break

            if not conversation_id:
                time.sleep(1)
                continue

            if state.get("conversation_status") in {"done", "completed", "finished", "ended"}:
                state["status"] = "finished-before-monitor"
                break

            try:
                asyncio.run(_monitor_ws(conversation_id))
            except Exception as e:
                state["error"] = str(e)
            break

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


def _start_twilio_hard_cap_watchdog(
    *,
    call_started_at: datetime,
    to_number: str,
    max_duration_secs: int,
    state: dict,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """
    Enforce a hard call cap by ending the matching Twilio call after max_duration_secs.
    """
    if not twilio_configured():
        state["status"] = "disabled"
        state["error"] = "TWILIO_ACCOUNT_SID/TWILIO_AUTH_TOKEN missing"
        return None

    if not to_number or max_duration_secs <= 0:
        state["status"] = "disabled"
        state["error"] = "invalid to_number or max_duration_secs"
        return None

    started_at_utc = call_started_at if call_started_at.tzinfo else call_started_at.replace(tzinfo=timezone.utc)
    started_at_utc = started_at_utc.astimezone(timezone.utc)

    def _runner():
        state["status"] = "watching"
        state["max_duration_secs"] = int(max_duration_secs)
        state["to_number"] = to_number
        deadline = time.time() + int(max_duration_secs)
        sid = ""

        # Try to discover the active call SID while the timer is running.
        while time.time() < deadline and not stop_event.is_set():
            if not sid:
                try:
                    sid = newest_active_call_sid(to_number=to_number, started_after=started_at_utc)
                    if sid:
                        state["call_sid"] = sid
                        print(f"  [Twilio watchdog] tracking call sid={sid}")
                except Exception as e:
                    state["error"] = str(e)
            time.sleep(2)

        if stop_event.is_set():
            state["status"] = "stopped"
            return

        # Cap reached. Try ending tracked SID first; if missing, resolve once more.
        if not sid:
            try:
                sid = newest_active_call_sid(to_number=to_number, started_after=started_at_utc)
            except Exception as e:
                state["error"] = str(e)

        if not sid:
            state["status"] = "no_call_found_at_cap"
            return

        try:
            ok = end_call(sid)
            state["call_sid"] = sid
            state["status"] = "ended" if ok else "end_failed"
            if ok:
                print(f"  [Twilio watchdog] hard cap reached ({max_duration_secs}s) — ended call sid={sid}")
            else:
                print(f"  [Twilio watchdog] hard cap reached ({max_duration_secs}s) — failed to end sid={sid}")
        except Exception as e:
            state["status"] = "end_failed"
            state["error"] = str(e)
            print(f"  [Twilio watchdog] end error: {e}")

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    return thread


def _call_recording_public_secret() -> str:
    return (
        os.environ.get("CALL_RECORDING_PUBLIC_SECRET", "").strip()
        or DAEMON_TOKEN
        or os.environ.get("ELEVENLABS_API_KEY", "").strip()
    )


def _call_recording_token(conversation_id: str) -> str:
    secret = _call_recording_public_secret()
    if not secret:
        return ""
    return hmac.new(secret.encode("utf-8"), conversation_id.encode("utf-8"), hashlib.sha256).hexdigest()


def _public_call_recording_url(conversation_id: str) -> str:
    base = (
        os.environ.get("CALL_RECORDING_PUBLIC_BASE_URL", "").strip()
        or RAILWAY_URL
    ).rstrip("/")
    token = _call_recording_token(conversation_id)
    if not base or not token:
        return ""
    conv = urllib.parse.quote(conversation_id, safe="")
    return f"{base}/api/public/call-audio/{conv}.mp3?token={token}"


def _save_and_send_call_recording(conversation_id: str, test_id: str, test_name: str, call_transcript_result: dict | None = None) -> dict:
    """
    Send call audio recording and visually clean PDF analysis report to Asmi.
    Also sends the server-hosted ElevenLabs recording link to the command chat.
    """
    state = {"status": "not_started", "public_url": "", "sent": False, "error": ""}
    if not conversation_id:
        state["status"] = "skipped"
        state["error"] = "missing conversation_id"
        return state

    from config import REPORTS_DIR, ASMI_HANDLE
    from imessage import send_imessage_attachment

    # 1. Download ElevenLabs audio
    recording_path = os.path.join(REPORTS_DIR, f"recording_{conversation_id}.mp3")
    try:
        from elevenlabs_phone import get_conversation_audio
        audio_bytes, _ = get_conversation_audio(conversation_id)
        with open(recording_path, "wb") as f:
            f.write(audio_bytes)
        print(f"  [ElevenLabs audio] Downloaded recording to {recording_path}")
    except Exception as e:
        print(f"  [ElevenLabs audio] Failed to download recording: {e}")
        recording_path = None

    # 2. Generate analysis PDF
    pdf_path = os.path.join(REPORTS_DIR, f"analysis_{conversation_id}.pdf")
    try:
        from report import generate_call_analysis_pdf
        
        # If call_transcript_result was not passed, fetch it
        if not call_transcript_result:
            from elevenlabs_phone import get_conversation, _parse_conversation
            detail = get_conversation(conversation_id)
            call_transcript_result = _parse_conversation(detail)
            
        generate_call_analysis_pdf(
            call_data=call_transcript_result,
            test_id=test_id,
            test_name=test_name,
            call_phone=CALL_EVAL_PHONE,
            output_path=pdf_path,
        )
        print(f"  [ElevenLabs PDF] Generated analysis PDF at {pdf_path}")
    except Exception as e:
        print(f"  [ElevenLabs PDF] Failed to generate analysis PDF: {e}")
        pdf_path = None

    # 3. Always send the attachments (audio + PDF) to Asmi number (ASMI_HANDLE)
    attachments_sent = False
    asmi_number = ASMI_HANDLE or "+14082307921"
    print(f"  [iMessage Attachments] Attempting to send audio & PDF to Asmi at {asmi_number}…")
    
    audio_sent = False
    if recording_path and os.path.exists(recording_path):
        audio_sent = send_imessage_attachment(recording_path, handle=asmi_number)
        print(f"  [iMessage Attachments] Sent audio recording: {audio_sent}")
        
    pdf_sent = False
    if pdf_path and os.path.exists(pdf_path):
        pdf_sent = send_imessage_attachment(pdf_path, handle=asmi_number)
        print(f"  [iMessage Attachments] Sent analysis PDF: {pdf_sent}")
        
    attachments_sent = audio_sent or pdf_sent
    state["attachments_sent"] = attachments_sent

    # 3.5. Optionally send the call details to Slack if configured
    slack_token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    slack_channel = os.environ.get("SLACK_CHANNEL", "").strip()
    if slack_token and slack_channel:
        print("  [Slack] Triggering call dispatch to Slack channel…")
        try:
            from slack import send_call_to_slack
            slack_state = send_call_to_slack(
                conversation_id=conversation_id,
                test_id=test_id,
                test_name=test_name,
                call_transcript_result=call_transcript_result,
            )
            state["slack"] = slack_state
        except Exception as e:
            print(f"  [Slack] Dispatch failed: {e}")
            state["slack"] = {"error": str(e)}

    # 4. Standard public Render link logic for command chat
    try:
        public_url = _public_call_recording_url(conversation_id)
        if public_url:
            dest = os.environ.get("CALL_RECORDING_CHAT_HANDLE", "").strip() or COMMAND_HANDLE
            label = (
                f"Call recording for {test_id}: {test_name}\n"
                f"Conversation: {conversation_id}\n"
                f"{public_url}"
            )
            sent = send_imessage(label, handle=dest)
            state["public_url"] = public_url
            state["sent"] = bool(sent)
            state["status"] = "link_sent" if sent else "link_send_failed"
            print(f"  [ElevenLabs audio] public link → {public_url}")
            print(f"  [ElevenLabs audio] link sent to chat={dest}: {sent}")
        else:
            state["status"] = "link_skipped"
            state["error"] = "missing CALL_RECORDING_PUBLIC_BASE_URL/REMOTE_UI_URL or signing secret"
    except Exception as e:
        state["status"] = "error"
        state["error"] = str(e)
        print(f"  [ElevenLabs audio] error: {e}")
    return state


def _missing_call_transcript_note(result: dict, tc: dict) -> str:
    """Give Gemini explicit evidence when ElevenLabs did not return a transcript."""
    monitor = result.get("call_monitor_state") or {}
    monitor_log = (result.get("call_monitor_log") or "").strip()
    twilio = result.get("twilio_hard_cap") or {}
    recording = result.get("call_recording_chat") or {}
    criteria = (tc.get("pass_criteria") or "").strip()
    return (
        "NO ELEVENLABS CALL TRANSCRIPT CAPTURED.\n"
        "Evaluate using the iMessage transcript, captured Asmi responses, live monitor state, "
        "recording status, Twilio watchdog state, and pass criteria below.\n"
        "If the pass criteria require proof that the phone call completed or produced a real outcome, "
        "do not infer success unless the available evidence proves it.\n\n"
        f"Pass criteria: {criteria or '(none)'}\n"
        f"Live monitor state: {json.dumps(monitor, ensure_ascii=False)}\n"
        f"Live monitor log: {monitor_log or '(none)'}\n"
        f"Twilio watchdog state: {json.dumps(twilio, ensure_ascii=False)}\n"
        f"Recording status: {json.dumps(recording, ensure_ascii=False)}"
    )


def _skip_ids() -> set[str]:
    path = os.environ.get("ASMI_SKIP_FILE", "").strip()
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(x).strip() for x in data if str(x).strip()}
    except Exception:
        return set()


def _interactive_auto_continue(tc: dict) -> bool:
    env = os.environ.get("ASMI_INTERACTIVE_AUTO_CONTINUE")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    return bool(tc.get("auto_continue", True))


def _split_lines(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        out = []
        for v in val:
            out.extend(_split_lines(v))
        return out
    if isinstance(val, str):
        s = val.replace("\\n", "\n")
        return [line.strip() for part in s.split("|") for line in part.splitlines() if line.strip()]
    return [str(val).strip()] if str(val).strip() else []

def _messages_to_send(tc: dict) -> list[str]:
    """
    Best-effort reconstruction of the exact user messages a test will send.
    Used for runner-side setup decisions (e.g., avoid duplicate cmd_onboard).
    """
    tc = tc or {}
    t = str(tc.get("type") or "").strip()

    if t == "single":
        return _split_lines(tc.get("start_message") or tc.get("message"))
    if t in {"burst", "sequence"}:
        return _split_lines(tc.get("messages"))
    if t == "burst_with_setup":
        setup = _split_lines(tc.get("setup_message"))
        msgs = _split_lines(tc.get("messages"))
        return setup + msgs
    if t == "dedup":
        msg1 = _split_lines(tc.get("message"))
        msg2 = _split_lines(tc.get("dedup_message") or tc.get("message"))
        return msg1 + msg2
    if t == "interactive":
        start = _split_lines(tc.get("start_message") or tc.get("message"))
        followups = _split_lines(tc.get("followups") or tc.get("messages") or tc.get("replies"))
        return start + followups

    return _split_lines(tc.get("start_message") or tc.get("message") or tc.get("messages"))

def _group_raw_responses_by_turn(sent_turns: list[dict], raw_msgs: list[dict]) -> list[dict]:
    """
    Assign each raw assistant message to the most recent user turn whose
    started_at is <= message timestamp. Returns turns with responses filled.
    """
    turns = []
    for t in sent_turns:
        turns.append({
            "turn": t["turn"],
            "user": t["user"],
            "responses": [],
            "started_at": t["started_at"],
            "finished_at": t.get("finished_at") or t["started_at"],
        })

    # Ensure chronological order for assignment.
    raw_msgs = sorted(raw_msgs or [], key=lambda m: (m.get("timestamp") or ""))
    for m in raw_msgs:
        ts = m.get("timestamp")
        text = (m.get("text") or "").strip()
        if not ts or not text:
            continue
        # Find the latest turn whose started_at <= ts.
        idx = None
        for i in range(len(turns) - 1, -1, -1):
            if turns[i]["started_at"] <= ts:
                idx = i
                break
        if idx is None:
            continue
        turns[idx]["responses"].append(text)
        # Track last response time for display sorting.
        turns[idx]["finished_at"] = max(turns[idx]["finished_at"], ts)
    return turns


def reconstruct_transcript(session_start: datetime, raw_msgs: list[dict], default_user: str = None) -> list[dict]:
    """
    Reconstruct the transcript turns chronologically.
    Every user message (is_from_me = True) starts a new turn.
    Assistant replies (is_from_me = False) are grouped under the most recent user turn.
    If there are no user messages in raw_msgs but default_user is provided,
    we seed a single turn with default_user.
    """
    turns = []
    # Ensure raw_msgs are sorted chronologically
    sorted_msgs = sorted(raw_msgs or [], key=lambda m: m.get("timestamp") or datetime.min)
    
    current_turn = None
    turn_counter = 1
    
    for m in sorted_msgs:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        ts = m.get("timestamp")
        ts_iso = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        
        if m.get("is_from_me"):
            # Dedup consecutive identical user messages (e.g. from manual tracking + SQLite)
            if current_turn and current_turn["user"] == text:
                try:
                    prev_ts = datetime.fromisoformat(current_turn["started_at"])
                    ts_dt = ts if isinstance(ts, datetime) else datetime.fromisoformat(ts)
                    if abs((ts_dt - prev_ts).total_seconds()) < 30.0:
                        continue
                except Exception:
                    pass

            # It's a user message (automated or manual)
            current_turn = {
                "turn": turn_counter,
                "user": text,
                "responses": [],
                "started_at": ts_iso,
                "finished_at": ts_iso,
            }
            turns.append(current_turn)
            turn_counter += 1
        else:
            # It's an assistant response
            if current_turn is None:
                # Assistant replied before any user message was recorded in raw_msgs.
                # If default_user is provided, we can seed the first turn with it.
                user_text = default_user or "<System / Context>"
                start_time_iso = session_start.isoformat() if isinstance(session_start, datetime) else str(session_start)
                current_turn = {
                    "turn": turn_counter,
                    "user": user_text,
                    "responses": [],
                    "started_at": start_time_iso,
                    "finished_at": ts_iso,
                }
                turns.append(current_turn)
                turn_counter += 1
            current_turn["responses"].append(text)
            current_turn["finished_at"] = max(current_turn["finished_at"], ts_iso)
            
    if not turns and default_user:
        start_time_iso = session_start.isoformat() if isinstance(session_start, datetime) else str(session_start)
        turns.append({
            "turn": 1,
            "user": default_user,
            "responses": [],
            "started_at": start_time_iso,
            "finished_at": start_time_iso,
        })
        
    return turns


# ── Phase 1: collect responses ─────────────────────────────────────────────────

def collect(tc: dict) -> dict:
    """
    Send iMessages for one test case and collect responses.
    Does NOT call Gemini — just records raw responses.
    """
    test_id   = tc["id"]
    test_type = tc["type"]

    print(f"\n  [{test_id}] {tc['name']}")
    if tc.get("precondition"):
        print(f"  ⚠ Precondition: {tc['precondition']}")

    result = {
        "id":           test_id,
        "name":         tc["name"],
        "category":     tc["category"],
        "type":         test_type,
        "tasks_sent":   [],
        "responses":    [],
        "verdict":      "UNCLEAR",
        "reason":       "Pending judge",
        "count_verdict": None,
        "manual_check": tc.get("manual_check"),
        "note":         tc.get("note"),
        "started_at":   datetime.now(timezone.utc).isoformat(),
    }
    stopped_early = False

    # ── Channel routing ──────────────────────────────────────────────────────
    # Resolve send / wait helpers based on tc['channel'] (default: imessage).
    # All test types use send_fn / wait_fn / catch_up_fn so existing tests are
    # completely unaffected — they still run over iMessage.
    _channel = (tc.get("channel") or "imessage").lower().strip()
    _asmi_target = (os.environ.get("ASMI_TARGET") or os.environ.get("ASMI_HANDLE") or "").strip()
    if _channel == "whatsapp":
        # Pick prod or dev WhatsApp handle based on the target env
        _wa_handle = WHATSAPP_PROD_HANDLE if "prod" in _asmi_target else WHATSAPP_DEV_HANDLE
        _wa_handle = tc.get("whatsapp_handle") or _wa_handle  # test-level override
        send_fn       = lambda msg: send_whatsapp(msg, _wa_handle)
        wait_fn       = lambda sent_at, **kw: wait_for_whatsapp_responses(sent_at, handle=_wa_handle, **kw)
        catch_up_fn   = lambda sent_at, seen_keys: catch_up_whatsapp_messages(sent_at, seen_keys, handle=_wa_handle)
        result["channel"] = "whatsapp"
        result["whatsapp_handle"] = _wa_handle
        print(f"  [channel=whatsapp] target handle: {_wa_handle}")
    else:
        send_fn     = send_imessage
        wait_fn     = wait_for_responses
        catch_up_fn = catch_up_manual_messages
        result["channel"] = "imessage"
    # ─────────────────────────────────────────────────────────────────────────

    if test_type == "single":
        if _stop_requested():
            stopped_early = True
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        msg = (tc.get("start_message") or tc.get("message") or "").strip()
        if not msg:
            raise KeyError(f"Test {test_id} (type=single) missing message/start_message")
        expected = int(tc.get("expected_responses") or 1)
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))

        result["tasks_sent"] = [msg]
        print(f"  --> {msg[:80]}")
        sent_at = datetime.now(timezone.utc)
        ok = send_fn(msg)
        if not ok:
            result["responses"] = []
            result["transcript"] = reconstruct_transcript(sent_at, [], default_user=msg)
        else:
            is_cmd = msg.strip().startswith("cmd_")
            raw = wait_fn(
                sent_at,
                count=expected,
                timeout=10 if is_cmd else wait,
                max_responses=max_responses,
                drain_all=False if is_cmd else True,
                return_raw=True,
                silence_after=2.0 if is_cmd else silence_after,
            )
            transcript = reconstruct_transcript(sent_at, raw, default_user=msg)
            result["transcript"] = transcript
            result["tasks_sent"] = [t["user"] for t in transcript]
            result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
            cv = judge_response_count(tc["name"], result["responses"], expected)
            result["count_verdict"] = cv

    elif test_type == "burst":
        msgs     = _split_lines(tc["messages"])
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = msgs
        burst_delay = tc.get("burst_delay", BURST_SEND_DELAY)
        wait = tc.get("wait", BURST_WAIT)
        session_start = None
        sent_success_count = 0
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            print(f"  --> Sending [{i+1}/{len(msgs)}]: {msg[:70]}")
            ok = send_fn(msg)
            if not ok:
                stopped_early = True
                result["reason"] = f"Send failed at burst message {i+1}/{len(msgs)}."
                print(f"  ⚠ {result['reason']}")
                break
            sent_success_count += 1
            if i < len(msgs) - 1:
                delay = _next_message_delay(msg, burst_delay)
                print(f"  (waiting {delay}s before next message…)")
                if not _sleep_interruptible(delay):
                    stopped_early = True
                    break
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        session_start = session_start or datetime.now(timezone.utc)
        if sent_success_count > 0:
            wait_expected = min(int(expected), sent_success_count)
            raw = wait_for_responses(
                session_start,
                count=max(1, wait_expected),
                timeout=wait,
                max_responses=max(10, expected + 6),
                drain_all=True,
                return_raw=True,
                silence_after=silence_after,
            )
        else:
            raw = []
        transcript = reconstruct_transcript(session_start, raw, default_user=msgs[0] if msgs else None)
        result["transcript"] = transcript
        result["tasks_sent"] = [t["user"] for t in transcript]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "burst_with_setup":
        setup = tc["setup_message"]
        msgs  = _split_lines(tc["messages"])
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = [setup] + msgs
        if _stop_requested():
            stopped_early = True
            result["tasks_sent"] = []
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        print(f"  → Setup: {setup}")
        setup_sent = datetime.now(timezone.utc)
        setup_ok = send_fn(setup)
        if not setup_ok:
            stopped_early = True
            result["reason"] = "Send failed for setup message."
            result["transcript"] = reconstruct_transcript(setup_sent, [], default_user=setup)
            result["tasks_sent"] = [setup]
            result["responses"] = []
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⚠ Send failed for setup message")
            return result
        setup_delay = _next_message_delay(setup, tc.get("setup_wait", 20))
        print(f"  (waiting {setup_delay}s before burst messages…)")
        if not _sleep_interruptible(setup_delay):
            stopped_early = True
            result["transcript"] = reconstruct_transcript(setup_sent, [], default_user=setup)
            result["tasks_sent"] = [setup]
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — setup sent, not sending burst messages")
            return result
        burst_delay = tc.get("burst_delay", BURST_SEND_DELAY)
        wait = tc.get("wait", BURST_WAIT)
        session_start = setup_sent
        sent_success_count = 0
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            print(f"  → Sending [{i+1}/{len(msgs)}]: {msg[:70]}")
            ok = send_fn(msg)
            if not ok:
                stopped_early = True
                result["reason"] = f"Send failed at burst message {i+1}/{len(msgs)}."
                print(f"  ⚠ {result['reason']}")
                break
            sent_success_count += 1
            if i < len(msgs) - 1:
                if not _sleep_interruptible(burst_delay):
                    stopped_early = True
                    break
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        if sent_success_count > 0:
            wait_expected = min(int(expected), sent_success_count)
            raw = wait_for_responses(
                session_start,
                count=max(1, wait_expected),
                timeout=wait,
                max_responses=max(10, expected + 6),
                drain_all=True,
                return_raw=True,
                silence_after=silence_after,
            )
        else:
            raw = []
        transcript = reconstruct_transcript(session_start, raw, default_user=setup)
        result["transcript"] = transcript
        result["tasks_sent"] = [t["user"] for t in transcript]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "sequence":
        msgs = _split_lines(tc["messages"])
        expected = int(tc.get("expected_responses") or len(msgs))
        result["tasks_sent"] = msgs
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))
        sequence_delay = tc.get("sequence_delay", SEQUENCE_DELAY)

        session_start = None
        seen_keys = set()
        all_raw = []
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            print(f"\\n  → Step [{i+1}/{len(msgs)}]: {msg[:70]}")
            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            
            all_raw.append({
                "text": msg,
                "timestamp": sent_at,
                "is_from_me": True
            })
            
            ok = send_imessage(msg)
            if ok:
                is_cmd = msg.strip().startswith("cmd_")
                raw = wait_for_responses(
                    session_start,
                    count=1,
                    timeout=10 if is_cmd else wait,
                    max_responses=max_responses,
                    drain_all=False if is_cmd else True,
                    return_raw=True,
                    silence_after=2.0 if is_cmd else silence_after,
                    seen_keys=seen_keys,
                )
                for m in raw or []:
                    all_raw.append(m)
                # Catch any manual user messages that synced late to chat.db
                late = catch_up_manual_messages(session_start, seen_keys)
                all_raw.extend(late)
            else:
                stopped_early = True
                result["reason"] = f"Send failed at sequence step {i+1}/{len(msgs)}."
                print(f"  ⚠ {result['reason']}")
                break
            if i < len(msgs) - 1:
                delay = _next_message_delay(msg, sequence_delay)
                print(f"  (waiting {delay}s before next message…)")
                if not _sleep_interruptible(delay):
                    stopped_early = True
                    break

        session_start = session_start or datetime.now(timezone.utc)
        # Sort chronologically so late-arriving manual messages are in the right position
        all_raw.sort(key=lambda m: m.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc))
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=msgs[0] if msgs else None)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in all_raw if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], [r for r in result["responses"] if r], expected)
        result["count_verdict"] = cv

    elif test_type == "dedup":
        msg1  = tc["message"]
        msg2  = tc.get("dedup_message", msg1)
        delay = tc.get("dedup_delay", 2.0)
        expected = tc.get("expected_responses", 1)
        result["tasks_sent"] = [msg1, msg2]
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))
        session_start = datetime.now(timezone.utc)
        all_raw = []
        if _stop_requested():
            stopped_early = True
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        print(f"  → Sending msg 1: {msg1[:70]}")
        all_raw.append({"text": msg1, "timestamp": datetime.now(timezone.utc), "is_from_me": True})
        ok1 = send_imessage(msg1)
        if not ok1:
            stopped_early = True
            result["reason"] = "Send failed for dedup message 1."
            print("  ⚠ Send failed for dedup message 1")
        next_delay = _next_message_delay(msg1, delay)
        print(f"  (waiting {next_delay}s before msg 2…)")
        if ok1 and _sleep_interruptible(next_delay):
            print(f"  → Sending msg 2: {msg2[:70]}")
            all_raw.append({"text": msg2, "timestamp": datetime.now(timezone.utc), "is_from_me": True})
            ok2 = send_imessage(msg2)
            if not ok2:
                stopped_early = True
                result["reason"] = "Send failed for dedup message 2."
                print("  ⚠ Send failed for dedup message 2")
        elif not ok1:
            pass
        else:
            stopped_early = True

        if ok1:
            raw = wait_for_responses(
                session_start,
                count=expected + 1,
                timeout=wait,
                max_responses=max_responses,
                drain_all=True,
                return_raw=True,
                silence_after=silence_after,
            )
        else:
            raw = []
        for m in raw or []:
            all_raw.append(m)
            
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=msg1)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        # Dedup count check
        actual = len([r for r in result["responses"] if r])
        if actual > expected:
            result["count_verdict"] = {"verdict": "FAIL",
                                       "reason": f"Dedup failed — got {actual} responses, expected {expected}."}
        elif actual == 0:
            result["count_verdict"] = {"verdict": "FAIL", "reason": "No response received."}
        else:
            result["count_verdict"] = {"verdict": "PASS", "reason": f"Got {actual}/{expected} response(s)."}

    elif test_type in {"interactive", "conversation"}:
        start_message = tc.get("start_message") or tc.get("message") or ""
        followups = _split_lines(tc.get("followups") or tc.get("messages") or tc.get("replies"))
        stop_when = _split_lines(tc.get("stop_when"))
        auto_continue = _interactive_auto_continue(tc)
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        max_turns = int(tc.get("max_turns") or max(2, len(followups) + 1))
        max_responses = int(tc.get("max_responses") or 10)
        
        session_start = None
        seen_keys = set()
        all_raw = []

        def _hit_stop(text: str) -> bool:
            if not stop_when:
                return False
            hay = (text or "").lower()
            return any(term.lower() in hay for term in stop_when)

        print(f"  → Start: {start_message[:80]}")
        current_user = start_message
        for turn_idx in range(max_turns):
            if _stop_requested():
                stopped_early = True
                break
            if not current_user:
                break

            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            
            # Send the automated user message
            ok = send_imessage(current_user)
            if not ok:
                stopped_early = True
                result["reason"] = f"Send failed at interactive turn {turn_idx+1}."
                print(f"  ⚠ {result['reason']}")
                break
            
            poll = wait_for_responses(
                session_start,
                count=1,
                timeout=wait,
                max_responses=max_responses,
                drain_all=True,
                return_raw=True,
                silence_after=8.0,
                seen_keys=seen_keys,
            )
            for m in poll or []:
                all_raw.append(m)

            # Get only the assistant responses for this step to check stop conditions
            step_responses = [m.get("text") for m in poll if m.get("text") and not m.get("is_from_me")]

            latest = " ".join(step_responses[-2:]) if step_responses else ""
            if _hit_stop(latest):
                result["verdict"] = "PASS"
                result["reason"] = "Interactive conversation reached the stop condition."
                break

            if not auto_continue:
                result["verdict"] = "UNCLEAR"
                result["reason"] = "Interactive run paused with auto-continue off."
                break

            if turn_idx >= len(followups):
                result["verdict"] = "UNCLEAR"
                result["reason"] = "Interactive follow-up script ended before the conversation naturally closed."
                break

            current_user = followups[turn_idx]

        session_start = session_start or datetime.now(timezone.utc)
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=start_message)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in all_raw if (m.get("text") or "").strip() and not m.get("is_from_me")]
        result["auto_continue"] = auto_continue
        result["max_turns"] = max_turns
        result["max_responses"] = max_responses

    elif test_type == "call_eval":
        # ── End-to-end call eval ──────────────────────────────────────────────
        # Sends iMessage(s) to Asmi to trigger a task that involves an outbound
        # call to CALL_EVAL_PHONE (a Twilio number). ElevenLabs answers as a
        # persona. After the call ends, we fetch the transcript from ElevenLabs.
        msgs = _split_lines(tc.get("messages") or tc.get("message"))
        persona_prompt = (tc.get("persona_prompt") or "").strip()
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or 10)
        sequence_delay = tc.get("sequence_delay", SEQUENCE_DELAY)
        call_timeout = max(1, min(int(tc.get("call_transcript_timeout") or CALL_TRANSCRIPT_TIMEOUT), 180))

        if not msgs:
            result["reason"] = "call_eval test is missing messages — add a messages list or message before running."
            result["verdict"] = "UNCLEAR"
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            return result

        # Substitute {{call_number}} placeholder with the configured Twilio number
        call_phone = CALL_EVAL_PHONE.strip()
        msgs = [
            m.replace("{{call_number}}", call_phone).replace("{{CALL_NUMBER}}", call_phone)
            for m in msgs
        ]

        if not call_phone:
            result["reason"] = "CALL_EVAL_PHONE is not configured — cannot run call_eval test."
            result["verdict"] = "UNCLEAR"
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            return result

        session_start = None
        seen_keys = set()
        all_raw = []
        call_started_at = None
        monitor_events: list[dict] = []
        monitor_state: dict = {"status": "not_started", "conversation_id": "", "conversation_status": "", "connected": False, "error": ""}
        monitor_stop = threading.Event()
        monitor_thread: threading.Thread | None = None
        twilio_watchdog_state: dict = {"status": "not_started", "max_duration_secs": int(CALL_EVAL_MAX_DURATION or 0), "to_number": call_phone, "call_sid": "", "error": ""}
        twilio_watchdog_stop = threading.Event()
        twilio_watchdog_thread: threading.Thread | None = None

        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            print(f"\n  → Step [{i+1}/{len(msgs)}]: {msg[:70]}")
            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at

            all_raw.append({"text": msg, "timestamp": sent_at, "is_from_me": True})

            # Mark the moment we send the message that triggers the call
            # (the last non-cmd_ message is most likely the one that causes the call)
            if not msg.strip().startswith("cmd_") and call_started_at is None:
                call_started_at = sent_at
                if monitor_thread is None:
                    agent_id = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
                    monitor_thread = _start_elevenlabs_monitor(agent_id, call_started_at, monitor_events, monitor_state, monitor_stop)
                if twilio_watchdog_thread is None and int(CALL_EVAL_MAX_DURATION or 0) > 0:
                    twilio_watchdog_thread = _start_twilio_hard_cap_watchdog(
                        call_started_at=call_started_at,
                        to_number=call_phone,
                        max_duration_secs=int(CALL_EVAL_MAX_DURATION or 0),
                        state=twilio_watchdog_state,
                        stop_event=twilio_watchdog_stop,
                    )

            ok = send_fn(msg)
            if ok:
                is_cmd = msg.strip().startswith("cmd_")
                is_last_call_eval = (test_type == "call_eval" and i == len(msgs) - 1 and not is_cmd)
                if is_last_call_eval:
                    # Short non-blocking wait: just capture Asmi's first
                    # acknowledgment message (e.g. "I've scheduled the call").
                    # All deep post-call response polling is done in the
                    # background by background_analyzer.py so the daemon loop
                    # is freed immediately to answer Slack commands.
                    print("  ⚡ call_eval last step — handing off to background analyzer after quick capture…")
                    raw = wait_fn(
                        sent_at=session_start,
                        count=1,
                        timeout=8,
                        max_responses=max_responses,
                        drain_all=False,
                        return_raw=True,
                        silence_after=3.0,
                        seen_keys=seen_keys,
                    )
                else:
                    raw = wait_fn(
                        sent_at=session_start,
                        count=1,
                        timeout=10 if is_cmd else wait,
                        max_responses=max_responses,
                        drain_all=False if is_cmd else True,
                        return_raw=True,
                        silence_after=2.0 if is_cmd else silence_after,
                        seen_keys=seen_keys,
                    )
                for m in raw or []:
                    all_raw.append(m)
                late = catch_up_manual_messages(session_start, seen_keys)
                all_raw.extend(late)
            else:
                stopped_early = True
                result["reason"] = f"Send failed at call_eval step {i+1}/{len(msgs)}."
                print(f"  ⚠ {result['reason']}")
                break
            if i < len(msgs) - 1:
                delay = _next_message_delay(msg, sequence_delay)
                print(f"  (waiting {delay}s before next message…)")
                if not _sleep_interruptible(delay):
                    stopped_early = True
                    break

        # Detached background call analysis trigger
        preferred_convo = (monitor_state.get("conversation_id") or "").strip()
        
        # Spawn detached background_analyzer.py
        import subprocess
        import sys
        
        # Clean up active monitor and watchdog threads
        if monitor_thread is not None:
            monitor_stop.set()
            monitor_thread.join(timeout=5)
        if twilio_watchdog_thread is not None:
            twilio_watchdog_stop.set()
            twilio_watchdog_thread.join(timeout=5)

        bg_analyzer_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "background_analyzer.py")
        started_iso = call_started_at.isoformat() if call_started_at else datetime.now(timezone.utc).isoformat()
        run_id = os.environ.get("ASMI_RUN_ID", "")
        asmi_target = os.environ.get("ASMI_TARGET", "").strip().lower()
        asmi_handle = os.environ.get("ASMI_HANDLE", "").strip()

        cmd = [
            sys.executable,
            bg_analyzer_path,
            "--test-id", test_id,
            "--call-started-after", started_iso,
            "--run-id", run_id,
            "--asmi-target", asmi_target,
            "--asmi-handle", asmi_handle,
        ]
        if preferred_convo:
            cmd.extend(["--preferred-convo-id", preferred_convo])

        print(f"\n  [Detached Spawn] Spawning background analyzer: {' '.join(cmd)}")
        try:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                start_new_session=True,
            )
        except Exception as e:
            print(f"  ⚠ Failed to spawn detached background analyzer: {e}")

        # Construct pending results and exit early
        session_start = session_start or datetime.now(timezone.utc)
        all_raw.sort(key=lambda m: m.get("timestamp") or datetime.min.replace(tzinfo=timezone.utc))
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=msgs[0] if msgs else None)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in all_raw if (m.get("text") or "").strip() and not m.get("is_from_me")]

        result["call_transcript"] = "ElevenLabs call started; analysis running in background."
        result["call_transcript_raw"] = []
        result["call_conversation_id"] = preferred_convo
        result["call_duration_secs"] = None
        result["call_recording_chat"] = {"status": "pending", "sent": False, "error": "Analysis running in background."}
        
        if monitor_events:
            result["call_monitor_events"] = monitor_events[-200:]
            result["call_monitor_log"] = "\n".join(
                f"[{idx+1:03d}] {evt.get('summary', '')}" for idx, evt in enumerate(monitor_events)
            )
        else:
            result["call_monitor_events"] = []
            result["call_monitor_log"] = ""
        result["call_monitor_state"] = monitor_state
        result["twilio_hard_cap"] = twilio_watchdog_state
        
        result["verdict"] = "PENDING"
        result["reason"] = "iMessages dispatched; call initiated and analysis running in background."

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    if stopped_early:
        result["reason"] = "Stopped early; judging responses captured so far."
    resp_count = len([r for r in result["responses"] if r])
    print(f"  ✓ Collected {resp_count} response(s)")
    return result


# ── Phase 2: batch judge ───────────────────────────────────────────────────────

def batch_judge(results: list[dict], all_responses: list[str], test_cases: list[dict]) -> list[dict]:
    """
    Run Gemini judge on all collected results.
    Fires ONE Gemini call per test with a delay between each.
    """
    tc_map = {t.get("id"): t for t in (test_cases or []) if t.get("id")}

    print(f"\n{'─'*65}")
    print(f"  JUDGING {len(results)} test(s) — {JUDGE_DELAY}s between calls (free tier)")
    print(f"  Est. time: ~{len(results) * JUDGE_DELAY // 60}m {len(results) * JUDGE_DELAY % 60}s")
    print(f"{'─'*65}")

    for i, r in enumerate(results):
        tc       = tc_map.get(r["id"], {})
        criteria = tc.get("pass_criteria", "")

        print(f"\n  [{i+1}/{len(results)}] Judging [{r['id']}]…", end=" ", flush=True)

        if not criteria:
            r["verdict"] = "UNCLEAR"
            r["reason"]  = "No pass_criteria in test_cases.py"
            print("⚠ skipped (no criteria)")
            continue

        # If count already failed, skip LLM and mark FAIL
        if r.get("count_verdict") and r["count_verdict"]["verdict"] == "FAIL":
            r["verdict"] = "FAIL"
            r["reason"]  = r["count_verdict"]["reason"]
            print(f"❌ FAIL (count check)")
            continue

        llm = judge_with_context(
            test_name     = r["name"],
            category      = r["category"],
            tasks         = r.get("tasks_sent", []),
            captured      = r.get("responses", []),
            all_responses = all_responses,
            pass_criteria = criteria,
            call_transcript = r.get("call_transcript"),
        )
        r["verdict"] = llm["verdict"]
        r["reason"]  = llm["reason"]
        if llm.get("matched_responses"):
            r["matched_responses"] = llm["matched_responses"]

        icon = {"PASS": "✅", "FAIL": "❌", "UNCLEAR": "⚠️"}.get(r["verdict"], "?")
        print(f"{icon} {r['verdict']}")

        status = judge_status()
        if not status["available"]:
            print(f"  Judge disabled for remaining tests: {status['reason']}")
            for rest in results[i + 1:]:
                if rest.get("verdict") in {"PASS", "FAIL"}:
                    continue
                rest["verdict"] = "UNCLEAR"
                rest["reason"] = status["reason"]
            break

        if i < len(results) - 1:
            time.sleep(JUDGE_DELAY)

    return results


# ── Collect all responses into a flat pool ─────────────────────────────────────

def _all_responses(results: list[dict]) -> list[str]:
    seen, pool = set(), []
    for r in results:
        for resp in r.get("responses", []):
            if resp and resp.strip() and resp not in seen:
                seen.add(resp)
                pool.append(resp.strip())
    return pool


# ── Main entry point ───────────────────────────────────────────────────────────

def _sort_by_priority(test_cases: list[dict]) -> list[dict]:
    """Sort tests by CATEGORY_RUN_ORDER, preserving original order within each category."""
    order = {cat: i for i, cat in enumerate(CATEGORY_RUN_ORDER)}
    return sorted(test_cases, key=lambda t: order.get(t["category"], len(CATEGORY_RUN_ORDER)))


def run_all(test_cases: list[dict], filter_category: str = None, filter_categories: list[str] = None, filter_id: str = None, filter_ids: list[str] = None) -> list[dict]:
    to_run = test_cases
    if filter_categories:
        to_run = [t for t in to_run if t["category"] in filter_categories]
    if filter_category:
        to_run = [t for t in to_run if t["category"] == filter_category]
    if filter_id:
        to_run = [t for t in to_run if t["id"] == filter_id]
    if filter_ids:
        to_run = [t for t in to_run if t["id"] in filter_ids]

    # Apply priority ordering only for full runs (no category/id filter)
    running_all = not filter_category and not filter_id and not filter_categories and not filter_ids
    if running_all:
        to_run = [t for t in to_run if t["category"] != "interactive"]
        to_run = _sort_by_priority(list(to_run))

    channels = {(t.get("channel") or "imessage").lower().strip() for t in to_run}
    channel_label = "mixed" if len(channels) > 1 else next(iter(channels), "imessage")

    print(f"\n{'═'*65}")
    print(f"  ASMI EVAL — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if running_all:
        print(f"  Priority order: {', '.join(CATEGORY_RUN_ORDER[:5])}…")
    print(f"  Phase 1: Sending {len(to_run)} test(s) via {channel_label}")
    print(f"  Phase 2: Batch judging at the end (Gemini free tier safe)")
    print(f"{'═'*65}")

    warmed_whatsapp_handles: set[str] = set()

    # Phase 1 — send all, collect responses
    results      = []
    last_category = None
    for tc in to_run:
        if tc["id"] in _skip_ids():
            print(f"\n  ⏭ Skipping [{tc['id']}] by request")
            continue
        if _stop_requested():
            print("\n  ⏹ Stop requested — judging captured results so far")
            break

        cat = tc["category"]
        channel = (tc.get("channel") or "imessage").lower().strip()

        # Before the first onboarding test in a full run, reset Asmi to fresh state
        if running_all and channel != "whatsapp" and cat == "onboarding" and last_category != "onboarding":
            # Avoid double-sending cmd_onboard if the first onboarding test
            # already starts with it.
            first_msg = (_messages_to_send(tc)[:1] or [""])[0]
            if first_msg == CMD_ONBOARD:
                print(f"\n  ━━━ Skipping extra cmd_onboard (test already starts with it) ━━━")
            else:
                print(f"\n  ━━━ Sending cmd_onboard to reset Asmi state ━━━")
                send_imessage(CMD_ONBOARD)
                print(f"  Waiting 10s for Asmi to reset…")
                time.sleep(10)

        if channel == "whatsapp":
            target_key = (os.environ.get("ASMI_TARGET") or os.environ.get("ASMI_HANDLE") or "").strip().lower()
            wa_handle = tc.get("whatsapp_handle") or (WHATSAPP_PROD_HANDLE if "prod" in target_key else WHATSAPP_DEV_HANDLE)
            if wa_handle and wa_handle not in warmed_whatsapp_handles:
                print(f"\n  ━━━ WhatsApp warmup for {wa_handle} ━━━")
                ensure_whatsapp_session(wa_handle)
                warmed_whatsapp_handles.add(wa_handle)

        # Print a separator when switching categories
        if cat != last_category:
            print(f"\n{'─'*65}")
            print(f"  CATEGORY: {cat.upper()}")
            print(f"{'─'*65}")
            last_category = cat

        try:
            r = collect(tc)
        except Exception as e:
            print(f"  ❌ Error collecting responses for {tc.get('id')}: {e}")
            r = {
                "id":           tc.get("id"),
                "name":         tc.get("name", "Unknown"),
                "category":     tc.get("category", "Unknown"),
                "type":         tc.get("type", "single"),
                "tasks_sent":   [],
                "responses":    [],
                "verdict":      "FAIL",
                "reason":       f"Setup error: {e}",
                "count_verdict": {"verdict": "FAIL", "reason": str(e)},
                "manual_check": tc.get("manual_check"),
                "note":         tc.get("note"),
                "started_at":   datetime.now(timezone.utc).isoformat(),
                "finished_at":  datetime.now(timezone.utc).isoformat(),
            }
        results.append(r)
        if _stop_requested():
            print("\n  ⏹ Stop requested — judging captured results so far")
            break
        time.sleep(5)  # brief pause between tests

    # Phase 2 — batch judge all at once
    pool    = _all_responses(results)
    results = batch_judge(results, all_responses=pool, test_cases=test_cases)

    # Summary
    total   = len(results)
    passed  = sum(1 for r in results if r["verdict"] == "PASS")
    failed  = sum(1 for r in results if r["verdict"] == "FAIL")
    other   = total - passed - failed

    print(f"\n{'═'*65}")
    print(f"  DONE: {passed} passed / {failed} failed / {other} unclear — {total} total")
    print(f"  Pass rate: {int(passed/total*100) if total else 0}%")
    print(f"{'═'*65}")

    if failed:
        print("\n  ❌ Failures:")
        for r in results:
            if r["verdict"] == "FAIL":
                print(f"     [{r['id']}] {r['name']}")
                print(f"       → {r['reason']}")

    return results
