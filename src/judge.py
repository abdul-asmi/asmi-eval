# ─── Gemini LLM Judge ─────────────────────────────────────────────────────────
# Uses google-genai SDK with gemini-3.1-flash-lite-preview

import google.genai as genai
import os
from config import GEMINI_API_KEY, GEMINI_MODEL

_client = None
_judge_disabled_reason = ""

SYSTEM_CONTEXT = """
You are evaluating responses from Asmi, an AI personal assistant accessible via iMessage.
Asmi handles real-world tasks: web research, restaurant/appointment/travel booking,
outbound phone calls to businesses, and sending emails.
Be strict but fair. Only mark PASS if the criteria are clearly met.

When evaluating outbound phone call tests, you will be provided with both the formatted plain-text transcript (voice call dialogue) and the ElevenLabs-provided evaluation analysis (structured metadata, success status, and data collection answers). You MUST evaluate both of these pieces of evidence coherently and together as a single unified context.
Do not treat the formatted plain-text transcript and the ElevenLabs-provided evaluation analysis as separate, isolated, or conflicting sources. Instead, cross-reference them to synthesize a complete and coherent picture of the call's outcome:
- Verify that what was spoken in the formatted plain-text transcript is accurately and consistently reflected in the ElevenLabs-provided evaluation analysis answers and data collection results.
- Use the structured metadata's success status and data collection answers to resolve or clarify any details that might be ambiguous or unclear in the plain-text dialogue transcript, and vice versa.
- A call passes ONLY when BOTH the formatted plain-text transcript and the ElevenLabs-provided evaluation analysis coherently and together demonstrate that the call criteria were fully achieved.

Control commands may appear in the task list. These are setup/reset/testing messages
that start with `cmd_` (examples below). They are NOT the user task being evaluated
unless the pass_criteria explicitly asks you to judge the command behavior.

Known control commands (examples):
- cmd_onboard: restart onboarding flow (quirks: may not reset timezone/name)
- cmd_onboard_skip: skip pre-onboarding (often run twice)
- cmd_reset_history: clear chat messages (profile preserved)
- cmd_message_then_call_mode: switch mode for check-in/reminder tests
- cmd_message_only_mode: switch to message-only (no calls)
- cmd_call_audio_test: generate a random call audio test
- cmd_user_call_legal: reset consent/legal flow
- cmd_el_voice: configure ElevenLabs voice style from a description
- cmd_el_voice_call: configure ElevenLabs call voice style from a description
"""


_PROMPT_WITH_CONTEXT = """
{system}

IMPORTANT: Tasks starting with `cmd_` are control commands (setup/reset) and should not be judged as the user request unless pass_criteria says so.

IMPORTANT CONTEXT: This eval ran 28 tests back-to-back in a single iMessage thread.
Asmi's responses may not be in order — a response captured during one test might
actually be answering an earlier or later task. Your job is to:
  1. Look through ALL responses from the entire run (provided below)
  2. Find which response(s) are actually answering the specific task(s) for this test
  3. Evaluate whether those matched responses meet the pass criteria

══ THIS TEST ══════════════════════════════════════════════════
Name: {test_name}
Category: {category}

Task(s) sent to Asmi:
{tasks}

Responses captured during this test's time window (may or may not be relevant):
{captured}

Pass criteria:
{pass_criteria}

══ FULL RESPONSE POOL (all {total} responses from entire run) ══════════════════
{all_responses}
══ CALL EVIDENCE (TRANSCRIPT & EVALUATION ANALYSIS) ═══════════════════════════════
{call_transcript_section}
════════════════════════════════════════════════════

Steps:
1. From the full pool, identify which response(s) are answering the task(s) above.
   A response is relevant if it directly addresses the task content.
2. If you find a relevant response: evaluate it against the pass criteria.
3. If voice call information is present, evaluate both the formatted plain-text transcript and the ElevenLabs-provided evaluation analysis coherently and together. Do not analyze or evaluate them separately or in isolation. You must synthesize the spoken conversation flow and the structured evaluation analysis metrics into a single, unified, coherent verdict.
4. If no response in the pool addresses this task: verdict is FAIL.

Reply in EXACTLY this format (no extra text):
MATCHED: Brief quote of the relevant response(s), or "none found"
VERDICT: PASS
REASON: One sentence.
(Replace PASS with FAIL or UNCLEAR as appropriate)
"""


def judge_with_context(test_name, category, tasks, captured, all_responses, pass_criteria,
                       call_transcript: str | None = None,
                       elevenlabs_analysis: dict | None = None):
    """
    Context-aware judge — gives Gemini the full response pool from the entire
    run so it can find which response actually answers this specific task,
    regardless of which test window it was captured in.
    Optionally includes the ElevenLabs call transcript and analysis metadata for call_eval tests.
    """
    valid_captured = [r for r in captured if r] if captured else []

    # Number and format all responses for the pool
    pool_lines = "\n".join(
        f"  [{i+1}] {r}" for i, r in enumerate(all_responses)
    )

    # Format call transcript section
    transcript_parts = []
    if elevenlabs_analysis:
        analysis_str = ""
        success_status = elevenlabs_analysis.get("call_successful")
        if success_status is not None:
            analysis_str += f"ElevenLabs Call Successful Status: {success_status}\n"
        agent_sent = elevenlabs_analysis.get("agent_sentiment")
        if agent_sent:
            analysis_str += f"Agent Sentiment: {agent_sent}\n"
        user_sent = elevenlabs_analysis.get("user_sentiment")
        if user_sent:
            analysis_str += f"User Sentiment: {user_sent}\n"
        summary = elevenlabs_analysis.get("transcript_summary")
        if summary:
            analysis_str += f"Transcript Summary: {summary}\n"
        
        data_collection = elevenlabs_analysis.get("data_collection_results") or {}
        if data_collection:
            analysis_str += "ElevenLabs Data Collection Answers:\n"
            for q_id, q_val in data_collection.items():
                if isinstance(q_val, dict):
                    q_text = q_val.get("question") or q_id
                    val = q_val.get("value")
                    rationale = q_val.get("rationale")
                    analysis_str += f"  - Question: {q_text}\n"
                    analysis_str += f"    Answer: {val}\n"
                    if rationale:
                        analysis_str += f"    Rationale: {rationale}\n"
                else:
                    analysis_str += f"  - {q_id}: {q_val}\n"
        if analysis_str:
            transcript_parts.append("=== ELEVENLABS-PROVIDED EVALUATION ANALYSIS ===\n" + analysis_str.strip() + "\n==============================================")

    if call_transcript and call_transcript.strip():
        transcript_parts.append(
            "=== FORMATTED PLAIN-TEXT TRANSCRIPT ===\n"
            "The following is the actual voice call transcript between Asmi (the AI agent)\n"
            "and the third-party persona (played by ElevenLabs):\n\n"
            + call_transcript
            + "\n========================================"
        )
    
    if transcript_parts:
        call_transcript_section = "\n\n".join(transcript_parts)
    else:
        call_transcript_section = (
            "NO ELEVENLABS CALL TRANSCRIPT WAS CAPTURED.\n"
            "Judge from the iMessage responses, full response pool, and pass criteria. "
            "If the criteria require proof of a completed phone call or confirmed call outcome, "
            "do not invent success without transcript, audio, monitor, or other concrete evidence."
        )

    prompt = _PROMPT_WITH_CONTEXT.format(
        system        = SYSTEM_CONTEXT,
        test_name     = test_name,
        category      = category,
        tasks         = _format_tasks(tasks),
        captured      = "\n".join(f"  - {r}" for r in valid_captured) if valid_captured else "  (none captured)",
        pass_criteria = pass_criteria,
        total         = len(all_responses),
        all_responses = pool_lines,
        call_transcript_section = call_transcript_section,
    )

    result = _call_gemini(prompt)

    # Parse out MATCHED line if present
    matched = None
    if "matched_responses" not in result:
        # try to extract from raw reason
        raw = result.get("_raw", "")
        for line in raw.splitlines():
            if line.strip().upper().startswith("MATCHED:"):
                matched = line.split(":", 1)[1].strip()
                break
    result["matched_responses"] = matched
    return result


def judge_status() -> dict:
    return {"available": not bool(_judge_disabled_reason), "reason": _judge_disabled_reason}


def _get_client():
    global _client
    key = os.environ.get("GEMINI_API_KEY", "").strip() or GEMINI_API_KEY
    if not key:
        raise RuntimeError("Gemini judge API key is not configured.")
    if _client is None:
        _client = genai.Client(api_key=key)
        return _client
    # If key rotated while process is alive, recreate client with latest key.
    try:
        current_key = getattr(getattr(_client, "_api_client", None), "api_key", None)
    except Exception:
        current_key = None
    if current_key != key:
        _client = genai.Client(api_key=key)
    return _client


def _normalize_judge_error(exc: Exception) -> tuple[str, bool]:
    raw = str(exc)
    lowered = raw.lower()

    if "api key was reported as leaked" in lowered:
        return "Judge unavailable: Gemini API key is blocked as leaked. Replace GEMINI_API_KEY and rerun judging.", True
    if "permission_denied" in lowered or "403" in lowered:
        return "Judge unavailable: Gemini rejected the API key or request permissions. Check GEMINI_API_KEY.", True
    if "api key" in lowered and ("invalid" in lowered or "expired" in lowered or "revoked" in lowered):
        return "Judge unavailable: Gemini API key is invalid or no longer active. Update GEMINI_API_KEY.", True
    if "not configured" in lowered:
        return raw, True
    return f"Judge error: {raw}", False


def _call_gemini(prompt: str) -> dict:
    global _judge_disabled_reason
    if _judge_disabled_reason:
        return {"verdict": "UNCLEAR", "reason": _judge_disabled_reason, "_raw": "", "judge_unavailable": True}

    try:
        client = _get_client()
        result  = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        text    = result.text.strip()
        verdict = "UNCLEAR"
        reason  = text

        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("VERDICT:"):
                raw = line.split(":", 1)[1].strip().upper()
                verdict = "PASS" if "PASS" in raw else "FAIL" if "FAIL" in raw else "UNCLEAR"
            elif line.upper().startswith("REASON:"):
                reason = line.split(":", 1)[1].strip()

        return {"verdict": verdict, "reason": reason, "_raw": text}

    except Exception as e:
        reason, disable_judge = _normalize_judge_error(e)
        if disable_judge:
            _judge_disabled_reason = reason
        return {"verdict": "UNCLEAR", "reason": reason, "_raw": "", "judge_unavailable": disable_judge}


def judge_response_count(test_name, responses, expected):
    actual = len([r for r in responses if r])
    if actual >= expected:
        return {"verdict": "PASS", "reason": f"Got {actual}/{expected} responses."}
    return {"verdict": "FAIL", "reason": f"Only got {actual}/{expected} responses — {expected - actual} dropped or timed out."}


def _format_tasks(tasks) -> str:
    out = []
    for i, t in enumerate(tasks or []):
        txt = str(t)
        if txt.strip().startswith("cmd_"):
            txt = f"{txt}  (CONTROL COMMAND: setup/reset/test)"
        out.append(f"  {i+1}. {txt}")
    return "\n".join(out) if out else "  (none)"
