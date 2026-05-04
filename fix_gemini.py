#!/usr/bin/env python3
"""
Run this once to find the working Gemini model and auto-patch config.py

    python fix_gemini.py
"""
import sys

API_KEY = "REDACTED_GEMINI_API_KEY"

# ── Step 1: try the new google-genai package first ────────────────────────────
try:
    import google.genai as genai
    client = genai.Client(api_key=API_KEY)
    print("✓ Using google-genai (new SDK)")

    models = []
    for m in client.models.list():
        if hasattr(m, 'name'):
            models.append(m.name)

    print(f"\nAvailable models ({len(models)}):")
    for m in models:
        print(f"  {m}")

    # pick first flash model
    preferred = [m for m in models if "flash" in m.lower()]
    chosen = preferred[0] if preferred else (models[0] if models else None)

    if chosen:
        # quick test
        resp = client.models.generate_content(model=chosen, contents="say: ok")
        print(f"\n✓ Test passed with: {chosen}")
        print(f"  Response: {resp.text.strip()}")
        _patch_config(chosen, sdk="new")
    else:
        print("No models found.")

except ImportError:
    # ── Step 2: fall back to google-generativeai ──────────────────────────────
    try:
        import google.generativeai as genai
        genai.configure(api_key=API_KEY)
        print("✓ Using google-generativeai (old SDK)")

        models = []
        for m in genai.list_models():
            if "generateContent" in m.supported_generation_methods:
                models.append(m.name)

        print(f"\nAvailable models ({len(models)}):")
        for m in models:
            print(f"  {m}")

        # pick first flash model
        preferred = [m for m in models if "flash" in m.lower()]
        chosen = preferred[0] if preferred else (models[0] if models else None)

        if chosen:
            model = genai.GenerativeModel(chosen)
            resp  = model.generate_content("say: ok")
            print(f"\n✓ Test passed with: {chosen}")
            print(f"  Response: {resp.text.strip()}")
            _patch_config(chosen, sdk="old")
        else:
            print("No models found.")

    except Exception as e:
        print(f"✗ google-generativeai also failed: {e}")
        sys.exit(1)

except Exception as e:
    print(f"✗ Error: {e}")
    sys.exit(1)


def _patch_config(model_name: str, sdk: str):
    """Rewrite config.py with the working model name."""
    with open("config.py") as f:
        content = f.read()

    # patch model name
    import re
    content = re.sub(
        r'GEMINI_MODEL\s*=\s*"[^"]+"',
        f'GEMINI_MODEL   = "{model_name}"',
        content
    )

    with open("config.py", "w") as f:
        f.write(content)

    print(f"\n✓ config.py patched → GEMINI_MODEL = \"{model_name}\"")

    # also patch judge.py if using new SDK
    if sdk == "new":
        _patch_judge_new_sdk()


def _patch_judge_new_sdk():
    """Rewrite judge.py to use the new google-genai SDK."""
    new_judge = '''# ─── Gemini LLM Judge (google-genai SDK) ──────────────────────────────────────
import google.genai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL

_client = genai.Client(api_key=GEMINI_API_KEY)

SYSTEM_CONTEXT = """
You are evaluating responses from Asmi, an AI personal assistant accessible via iMessage.
Asmi handles real-world tasks: web research, restaurant/appointment/travel booking,
outbound phone calls to businesses, and sending emails.
Be strict but fair. Only mark PASS if the criteria are clearly met.
"""

_PROMPT_TEMPLATE = """
{system}

══ TEST ══════════════════════════════
Name: {test_name}
Category: {category}

Task(s) sent to Asmi:
{tasks}

Asmi\'s response(s):
{responses}

Pass criteria:
{pass_criteria}
══════════════════════════════════════

Does Asmi\'s response meet the pass criteria above?

Reply in EXACTLY this format (no extra text):
VERDICT: PASS
REASON: One sentence.

or

VERDICT: FAIL
REASON: One sentence explaining what was wrong.

or

VERDICT: UNCLEAR
REASON: One sentence explaining what is ambiguous.
"""


def judge(test_name, category, tasks, responses, pass_criteria):
    if not responses or all(r is None for r in responses):
        return {"verdict": "FAIL", "reason": "No responses received from Asmi."}

    valid = [r for r in responses if r]
    prompt = _PROMPT_TEMPLATE.format(
        system        = SYSTEM_CONTEXT,
        test_name     = test_name,
        category      = category,
        tasks         = "\\n".join(f"  {i+1}. {t}" for i, t in enumerate(tasks)),
        responses     = "\\n".join(f"  {i+1}. {r}" for i, r in enumerate(valid)),
        pass_criteria = pass_criteria,
    )

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

        return {"verdict": verdict, "reason": reason}

    except Exception as e:
        return {"verdict": "UNCLEAR", "reason": f"Judge error: {e}"}


def judge_response_count(test_name, responses, expected):
    actual = len([r for r in responses if r])
    if actual >= expected:
        return {"verdict": "PASS", "reason": f"Got {actual}/{expected} responses."}
    return {"verdict": "FAIL", "reason": f"Only got {actual}/{expected} responses — {expected - actual} dropped or timed out."}
'''
    with open("judge.py", "w") as f:
        f.write(new_judge)
    print("✓ judge.py patched to use new google-genai SDK")
    print("\nNow run:  python rejudge.py results_20260504_1402.json")
