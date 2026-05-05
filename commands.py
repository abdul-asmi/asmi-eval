# ─── Command Executor ─────────────────────────────────────────────────────────
# Parses a text command and runs the right eval operation.
# Called by daemon.py when a new command message arrives.

import glob
import json
import os
import subprocess
import sys

import google.genai as genai
from config import GEMINI_API_KEY, GEMINI_MODEL, EVAL_DIR

_client = genai.Client(api_key=GEMINI_API_KEY)

CATEGORIES = [
    "sticky_message", "call_dedup", "call_summary", "language_pref",
    "location_memory", "onboarding", "capability", "threep_nudge",
]

HELP_TEXT = """
*Asmi Eval Commands*

!run all              → run full 28-test suite
!run [category]       → run one category
!run [test_id]        → run one specific test
!rejudge              → re-judge latest results (no new messages)
!status               → summary of last run
!list                 → list all test IDs
!add test [describe]  → add a new test case with Gemini
!menu                 → show this message

Categories:
  sticky_message · call_dedup · call_summary · language_pref
  location_memory · onboarding · capability · threep_nudge

Examples:
  !run call_dedup
  !run dedup_01
  !run all
  !add test - call to a business that goes to voicemail should report honestly
""".strip()


# ─── Router ───────────────────────────────────────────────────────────────────

def handle(text: str) -> str:
    """Main entry point. Takes raw command text, returns response string."""
    text  = text.strip()
    lower = text.lower().lstrip("!")

    if lower in ["help", "commands", "menu"]:
        return HELP_TEXT

    if lower in ["status", "results", "last run", "last results"]:
        return _status()

    if lower in ["list", "tests", "list tests"]:
        return _list_tests()

    if lower.startswith("rejudge"):
        return _rejudge()

    if lower.startswith("run ") or lower == "run" or lower == "run all":
        arg = lower.replace("run", "").strip()
        return _run(arg)

    if lower.startswith("add test") or lower.startswith("new test"):
        description = text.split(" ", 2)[-1] if len(text.split()) > 2 else ""
        return _add_test(description)

    # Fall back to Gemini to parse the intent
    return _parse_unknown(text)


# ─── Handlers ─────────────────────────────────────────────────────────────────

def _run(arg: str) -> str:
    """Run eval tests. arg can be empty (all), a category, or a test ID."""
    arg = arg.strip()

    if not arg or arg == "all":
        cmd = [sys.executable, "run_eval.py"]
        label = "full suite (28 tests)"
    elif arg in CATEGORIES:
        cmd = [sys.executable, "run_eval.py", "--category", arg]
        label = f"category: {arg}"
    else:
        # assume it's a test ID
        cmd = [sys.executable, "run_eval.py", "--id", arg]
        label = f"test: {arg}"

    try:
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=EVAL_DIR, timeout=600
        )
        output = result.stdout + result.stderr

        # Parse the exact results file path from run_eval.py's output and
        # write it to a pointer file so daemon.py reads the right file
        import re as _re
        m = _re.search(r'Raw results:\s*(results_\S+\.json)', output)
        if m:
            results_path = os.path.join(EVAL_DIR, m.group(1))
            try:
                with open(os.path.join(EVAL_DIR, '.latest_results_path'), 'w') as _f:
                    _f.write(results_path)
            except Exception:
                pass

        # Extract summary line from output
        summary = _extract_summary(output)
        report  = _latest_report()

        return (
            f"Run complete — {label}\n"
            f"{summary}\n"
            f"Report: {report or 'check eval folder'}"
        )
    except subprocess.TimeoutExpired:
        return f"⚠ Run timed out after 10 minutes — {label}"
    except Exception as e:
        return f"❌ Run failed: {e}"


def _rejudge() -> str:
    """Re-run Gemini judge on the latest results file."""
    results_files = sorted(
        glob.glob(os.path.join(EVAL_DIR, "results_[0-9]*.json")),
        reverse=True
    )
    if not results_files:
        return "❌ No results file found. Run tests first with !run all"

    latest = os.path.basename(results_files[0])
    try:
        result = subprocess.run(
            [sys.executable, "rejudge.py", latest],
            capture_output=True, text=True,
            cwd=EVAL_DIR, timeout=300
        )
        output  = result.stdout + result.stderr
        summary = _extract_summary(output)
        report  = _latest_report(prefix="report_rejudged")
        return (
            f"✅ Rejudge complete — {latest}\n"
            f"{summary}\n"
            f"Report: {report or 'check eval folder'}"
        )
    except Exception as e:
        return f"❌ Rejudge failed: {e}"


def _status() -> str:
    """Return summary of the most recent results file."""
    results_files = sorted(
        glob.glob(os.path.join(EVAL_DIR, "results_*.json")),
        reverse=True
    )
    if not results_files:
        return "No results yet. Run !run all to start."

    latest = results_files[0]
    with open(latest) as f:
        results = json.load(f)

    total   = len(results)
    passed  = sum(1 for r in results if r.get("verdict") == "PASS")
    failed  = sum(1 for r in results if r.get("verdict") == "FAIL")
    unclear = total - passed - failed
    pct     = int(passed / total * 100) if total else 0

    ts = os.path.basename(latest).replace("results_", "").replace(".json", "")

    lines = [
        f"📊 Last run: {ts}",
        f"{'✅' if pct == 100 else '⚠' if pct >= 70 else '❌'} {passed}/{total} passed ({pct}%)",
        f"Failed: {failed} · Unclear: {unclear}",
    ]

    # List failures
    fails = [r for r in results if r.get("verdict") == "FAIL"]
    if fails:
        lines.append("\n❌ Failures:")
        for r in fails:
            lines.append(f"  [{r['id']}] {r['name']}")

    return "\n".join(lines)


def _list_tests() -> str:
    """List all test IDs and names."""
    try:
        sys.path.insert(0, EVAL_DIR)
        from test_cases import TEST_CASES
        lines = [f"📋 {len(TEST_CASES)} tests:\n"]
        current_cat = None
        for t in TEST_CASES:
            if t["category"] != current_cat:
                current_cat = t["category"]
                lines.append(f"\n*{current_cat}*")
            pre = " ⚠" if t.get("precondition") else ""
            lines.append(f"  {t['id']}{pre} — {t['name']}")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Could not list tests: {e}"


def _add_test(description: str) -> str:
    """Use Gemini to generate a new test case from a natural language description."""
    if not description:
        return "❌ Describe the test: !add test [description]"

    prompt = f"""
You are helping add a test case to the Asmi eval system.
Asmi is a consumer AI assistant accessible via iMessage. It can make phone calls,
do web research, send emails, and book things.

The test_cases.py file uses this schema:
{{
    "id": "category_XX",
    "name": "Short descriptive name",
    "category": "one of: sticky_message|call_dedup|call_summary|language_pref|location_memory|onboarding|capability|threep_nudge",
    "type": "single|burst|sequence|dedup",
    "message": "The exact iMessage to send to Asmi",  # for single/dedup
    "messages": ["msg1", "msg2"],  # for burst/sequence
    "wait": 120,
    "pass_criteria": "Specific description of what a passing response looks like.",
    "manual_check": "optional - what to check outside iMessage",
    "precondition": "optional - required state"
}}

Generate a test case for this description:
"{description}"

Return ONLY valid Python dict syntax that can be pasted into the TEST_CASES list.
No explanation, no markdown fences — just the dict.
"""
    try:
        result = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        generated = result.text.strip().strip("```python").strip("```").strip()

        # Try to parse it
        import ast
        tc = ast.literal_eval(generated)

        # Append to test_cases.py
        test_cases_path = os.path.join(EVAL_DIR, "test_cases.py")
        with open(test_cases_path) as f:
            content = f.read()

        # Insert before the closing bracket of TEST_CASES
        insertion = f"\n    # Added via command daemon\n    {generated},\n"
        new_content = content.rstrip()
        if new_content.endswith("]"):
            new_content = new_content[:-1] + insertion + "]\n"
        else:
            return "❌ Could not auto-insert — paste manually into test_cases.py"

        with open(test_cases_path, "w") as f:
            f.write(new_content)

        return (
            f"✅ Test case added: [{tc.get('id')}] {tc.get('name')}\n"
            f"Category: {tc.get('category')} · Type: {tc.get('type')}\n"
            f"Run it with: !run {tc.get('id')}"
        )
    except Exception as e:
        return f"❌ Could not generate test case: {e}\n\nTry being more specific."


def _parse_unknown(text: str) -> str:
    """Use Gemini to interpret an unknown command."""
    prompt = f"""
The user sent this command to an eval daemon for the Asmi AI testing system:
"{text}"

Available commands: run all, run [category], run [test_id], rejudge, status, list, add test [description], help

What did they mean? Reply with ONLY the canonical command to execute (e.g. "run call_dedup")
or "unknown" if you can't tell.
"""
    try:
        result = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        parsed = result.text.strip().lower()
        if parsed == "unknown" or len(parsed) > 50:
            return f"❓ Unknown command: '{text}'\nType !help to see all commands."
        return handle("!" + parsed)
    except Exception:
        return f"❓ Unknown command: '{text}'\nType !help to see all commands."


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _extract_summary(output: str) -> str:
    for line in output.splitlines():
        if "passed" in line and "failed" in line:
            return line.strip()
    return "(see report for details)"


def _latest_report(prefix: str = "report") -> str:
    files = sorted(
        glob.glob(os.path.join(EVAL_DIR, f"{prefix}_*.html")),
        reverse=True
    )
    return os.path.basename(files[0]) if files else None
