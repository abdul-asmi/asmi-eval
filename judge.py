# ─── Gemini LLM Judge ─────────────────────────────────────────────────────────
# Uses google-genai SDK with gemini-3.1-flash-lite-preview

import google.genai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL

_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_CONTEXT = """
You are evaluating responses from Asmi, an AI personal assistant accessible via iMessage.
Asmi handles real-world tasks: web research, restaurant/appointment/travel booking,
outbound phone calls to businesses, and sending emails.
Be strict but fair. Only mark PASS if the criteria are clearly met.
"""

# ── Standard judge (direct responses) ─────────────────────────────────────────

_PROMPT = """
{system}

══ TEST ══════════════════════════════
Name: {test_name}
Category: {category}

Task(s) sent to Asmi:
{tasks}

Asmi's response(s):
{responses}

Pass criteria:
{pass_criteria}
══════════════════════════════════════

Does Asmi's response meet the pass criteria above?

Reply in EXACTLY this format:
VERDICT: PASS
REASON: One sentence.

or VERDICT: FAIL / VERDICT: UNCLEAR with REASON.
"""

# ── Context-aware judge (searches full response pool) ─────────────────────────

_PROMPT_WITH_CONTEXT = """
{system}

IMPORTANT CONTEXT: This eval ran 28 tests back-to-back in a single iMessage thread.
Asmi's responses may not be in order — a response captured during one test might
actually be answering an earlier or later task. Your job is to:
  1. Look through ALL responses from the entire run (provided below)
  2. Find which response(s) are actually answering the specific task(s) for this test
  3. Evaluate whether those matched responses meet the pass criteria

══ THIS TEST ════════════════════════════════════════
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
════════════════════════════════════════════════════

Steps:
1. From the full pool, identify which response(s) are answering the task(s) above.
   A response is relevant if it directly addresses the task content.
2. If you find a relevant response: evaluate it against the pass criteria.
3. If no response in the pool addresses this task: verdict is FAIL.

Reply in EXACTLY this format (no extra text):
MATCHED: Brief quote of the relevant response(s), or "none found"
VERDICT: PASS
REASON: One sentence.

(Replace PASS with FAIL or UNCLEAR as appropriate)
"""


def judge(test_name, category, tasks, responses, pass_criteria):
    """Standard judge — evaluates the directly captured responses."""
    if not responses or all(r is None for r in responses):
        return {"verdict": "FAIL", "reason": "No responses received from Asmi."}

    valid  = [r for r in responses if r]
    prompt = _PROMPT.format(
        system        = SYSTEM_CONTEXT,
        test_name     = test_name,
        category      = category,
        tasks         = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(tasks)),
        responses     = "\n".join(f"  {i+1}. {r}" for i, r in enumerate(valid)),
        pass_criteria = pass_criteria,
    )
    return _call_gemini(prompt)


def judge_with_context(test_name, category, tasks, captured, all_responses, pass_criteria):
    """
    Context-aware judge — gives Gemini the full response pool from the entire
    run so it can find which response actually answers this specific task,
    regardless of which test window it was captured in.
    """
    valid_captured = [r for r in captured if r] if captured else []

    # Number and format all responses for the pool
    pool_lines = "\n".join(
        f"  [{i+1}] {r}" for i, r in enumerate(all_responses)
    )

    prompt = _PROMPT_WITH_CONTEXT.format(
        system        = SYSTEM_CONTEXT,
        test_name     = test_name,
        category      = category,
        tasks         = "\n".join(f"  {i+1}. {t}" for i, t in enumerate(tasks)),
        captured      = "\n".join(f"  - {r}" for r in valid_captured) if valid_captured else "  (none captured)",
        pass_criteria = pass_criteria,
        total         = len(all_responses),
        all_responses = pool_lines,
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


def _call_gemini(prompt: str) -> dict:
    try:
        result  = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
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
        return {"verdict": "UNCLEAR", "reason": f"Judge error: {e}", "_raw": ""}


def judge_response_count(test_name, responses, expected):
    actual = len([r for r in responses if r])
    if actual >= expected:
        return {"verdict": "PASS", "reason": f"Got {actual}/{expected} responses."}
    return {"verdict": "FAIL", "reason": f"Only got {actual}/{expected} responses — {expected - actual} dropped or timed out."}
