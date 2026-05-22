# ─── ElevenLabs Conversational AI — Phone Call Transcript Helper ────────────────
#
# Used for `call_eval` test type.
# When Asmi places a call to the Twilio test number, ElevenLabs answers as a
# persona and conducts a voice conversation. After the call ends, this module
# fetches the full transcript from the ElevenLabs Conversations API.
#
# Required env vars:
#   ELEVENLABS_API_KEY   - your ElevenLabs API key (xi-...)
#   ELEVENLABS_AGENT_ID  - the agent that handles inbound calls
#
# Setup: In the ElevenLabs dashboard:
#   1. Create a Conversational AI agent (name it e.g. "Asmi Test Persona")
#   2. Under agent Security settings, enable "system_prompt" overrides
#   3. Import your Twilio number under Phone Numbers → Import → Twilio
#   4. Assign the agent to that Twilio number

import os
import time
import requests
from datetime import datetime, timezone

_BASE = "https://api.elevenlabs.io/v1"


def _api_key() -> str:
    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set — cannot fetch call transcript.")
    return key


def _headers() -> dict:
    return {"xi-api-key": _api_key(), "Content-Type": "application/json"}


def _agent_id() -> str:
    aid = os.environ.get("ELEVENLABS_AGENT_ID", "").strip()
    if not aid:
        raise RuntimeError("ELEVENLABS_AGENT_ID is not set — cannot identify which agent to poll.")
    return aid


def _stop_requested() -> bool:
    path = os.environ.get("ASMI_STOP_FILE", "").strip()
    return bool(path and os.path.exists(path))


def _sleep_interruptible(seconds: float) -> bool:
    """Sleep in short chunks. Returns False if the UI requested stop."""
    deadline = time.time() + max(0.0, float(seconds or 0))
    while time.time() < deadline:
        if _stop_requested():
            return False
        time.sleep(min(0.5, deadline - time.time()))
    return not _stop_requested()


def list_recent_conversations(agent_id: str, limit: int = 25) -> list[dict]:
    """
    Fetch the most recent conversations for the given agent.
    Returns list of conversation summary dicts from the ElevenLabs API.
    """
    url = f"{_BASE}/convai/conversations"
    params = {"agent_id": agent_id, "page_size": limit}
    resp = requests.get(url, headers=_headers(), params=params, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    # API returns {"conversations": [...], "has_more": bool, ...}
    return data.get("conversations") or []


def get_conversation(conversation_id: str) -> dict:
    """
    Fetch full conversation detail (including transcript) for a given conversation_id.
    """
    url = f"{_BASE}/convai/conversations/{conversation_id}"
    resp = requests.get(url, headers=_headers(), timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_conversation_audio(conversation_id: str) -> tuple[bytes, str]:
    """
    Fetch the raw audio recording for a given conversation_id.
    Returns (audio_bytes, content_type).
    """
    url = f"{_BASE}/convai/conversations/{conversation_id}/audio"
    resp = requests.get(url, headers=_headers(), timeout=30)
    resp.raise_for_status()
    content_type = resp.headers.get("Content-Type", "audio/mpeg")
    return resp.content, content_type


def wait_for_call_transcript(
    call_started_after: datetime,
    agent_id: str | None = None,
    preferred_conversation_id: str | None = None,
    timeout: int = 180,
    poll_interval: int = 5,
) -> dict | None:
    """
    Poll ElevenLabs for a new conversation that started after `call_started_after`.
    Waits until the conversation is in a terminal status (done/completed/failed).

    Returns a dict with keys:
        conversation_id  - str
        status           - str
        transcript       - list of {role, message, time_in_call_secs}
        transcript_text  - formatted string ready for the judge
        duration_secs    - int or None
    Or None if timeout is reached without finding a completed call.
    """
    agent_id = (agent_id or _agent_id()).strip()
    started_ts = call_started_after.replace(tzinfo=timezone.utc) if call_started_after.tzinfo is None else call_started_after.astimezone(timezone.utc)

    deadline = time.time() + timeout
    found_id = (preferred_conversation_id or "").strip() or None
    last_sig = ""
    unchanged_polls = 0
    print(f"\n  [ElevenLabs] Waiting up to {timeout}s for call transcript (agent={agent_id})…", end="", flush=True)

    while time.time() < deadline:
        if _stop_requested():
            print("\n  [ElevenLabs] Stop requested — using transcript captured so far")
            break
        if not _sleep_interruptible(poll_interval):
            print("\n  [ElevenLabs] Stop requested — using transcript captured so far")
            break
        print(".", end="", flush=True)

        # If we already know the conversation id, poll details directly.
        if found_id:
            try:
                detail = get_conversation(found_id)
            except Exception as e:
                print(f"\n  [ElevenLabs] detail fetch error (id={found_id}): {e}")
                continue

            parsed = _parse_conversation(detail)
            status = str(parsed.get("status") or detail.get("status") or "").lower().strip()
            transcript = parsed.get("transcript") or []
            parsed["status"] = status

            sig = "|".join(
                f"{t.get('role')}::{t.get('time_in_call_secs')}::{t.get('message')}"
                for t in transcript
            )
            if sig == last_sig:
                unchanged_polls += 1
            else:
                last_sig = sig
                unchanged_polls = 0

            if status in {"done", "completed", "finished", "ended", "failed", "error", "aborted"}:
                print(f"\n  [ElevenLabs] Call finished (status={status}) — transcript ready (id={found_id})")
                return parsed

            # Some calls never expose a terminal status promptly. If transcript
            # stops changing for multiple polls, treat it as settled and proceed.
            if transcript and unchanged_polls >= 2:
                print(f"\n  [ElevenLabs] Transcript settled (status={status or 'unknown'}) — proceeding (id={found_id})")
                return parsed

        try:
            convos = list_recent_conversations(agent_id)
        except Exception as e:
            print(f"\n  [ElevenLabs] list error: {e}")
            continue

        for convo in convos:
            # Each convo summary has start_time_unix_secs (or similar)
            # Field names vary slightly by API version — handle both.
            raw_start = (
                convo.get("start_time_unix_secs")
                or convo.get("started_at_unix_secs")
                or convo.get("created_at_unix_secs")
                or 0
            )
            if raw_start:
                convo_start = datetime.fromtimestamp(raw_start, tz=timezone.utc)
            else:
                # Fall back to ISO string if present
                iso = convo.get("start_time") or convo.get("started_at") or ""
                if iso:
                    try:
                        convo_start = datetime.fromisoformat(iso.replace("Z", "+00:00"))
                    except Exception:
                        continue
                else:
                    continue

            if convo_start < started_ts:
                # Older than our test — skip
                continue

            # This convo started AFTER our test — track it
            found_id = found_id or convo.get("conversation_id") or convo.get("id")
            status = (convo.get("status") or "").lower()

            if status in {"done", "completed", "finished", "ended"}:
                print(f"\n  [ElevenLabs] Call finished — fetching transcript (id={found_id})")
                try:
                    detail = get_conversation(found_id)
                    return _parse_conversation(detail)
                except Exception as e:
                    print(f"\n  [ElevenLabs] transcript fetch error: {e}")
                    return None

            # Found but still in progress — keep polling
            print(f"\n  [ElevenLabs] Call in progress (status={status}, id={found_id})…", end="", flush=True)
            break  # Don't check other older convos

    if found_id:
        # Timed out but found the call — try fetching anyway
        print(f"\n  [ElevenLabs] Timeout — fetching transcript anyway (id={found_id})")
        try:
            detail = get_conversation(found_id)
            return _parse_conversation(detail)
        except Exception as e:
            print(f"\n  [ElevenLabs] final transcript fetch error: {e}")

    print(f"\n  [ElevenLabs] ⚠ No call found within {timeout}s timeout")
    return None


def _parse_conversation(detail: dict) -> dict:
    """
    Parse an ElevenLabs conversation detail response into a normalized dict.
    """
    transcript_raw = detail.get("transcript") or []
    # Normalize to list of {role, message, time_in_call_secs}
    normalized = []
    for turn in transcript_raw:
        role = str(turn.get("role") or turn.get("speaker") or "unknown").lower()
        text = str(turn.get("message") or turn.get("text") or "").strip()
        t_secs = turn.get("time_in_call_secs") or turn.get("time_secs") or 0
        if text:
            normalized.append({"role": role, "message": text, "time_in_call_secs": t_secs})

    metadata = detail.get("metadata") or {}
    duration = (
        metadata.get("call_duration_secs")
        or detail.get("call_duration_secs")
        or detail.get("duration_secs")
    )

    return {
        "conversation_id": detail.get("conversation_id") or detail.get("id", ""),
        "status": detail.get("status", ""),
        "transcript": normalized,
        "transcript_text": format_call_transcript(normalized),
        "duration_secs": duration,
    }


def format_call_transcript(transcript: list[dict]) -> str:
    """
    Format a normalized transcript list into a readable string for the judge.
    Roles: 'agent' = ElevenLabs persona (third party), 'user' = Asmi.
    """
    if not transcript:
        return "(no transcript captured)"
    lines = []
    for turn in transcript:
        role = turn.get("role", "unknown")
        msg = turn.get("message", "").strip()
        t = turn.get("time_in_call_secs", 0)
        label = "Asmi" if role in {"user", "caller"} else "3rd Party (ElevenLabs persona)"
        lines.append(f"[{int(t):>3}s] {label}: {msg}")
    return "\n".join(lines)
