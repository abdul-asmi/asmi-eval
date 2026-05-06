#!/usr/bin/env python3
"""
Asmi Eval — Test Case Editor UI
Reads/writes test_cases.py via GitHub API so it works hosted on Railway.

Env vars required:
  GITHUB_TOKEN      — personal access token (repo scope)
  GITHUB_REPO       — e.g. "abdul-asmi/asmi-eval"
  GITHUB_FILE_PATH  — e.g. "test_cases.py"
  PORT              — optional, defaults to 8765
"""

import ast
import base64
import glob
import json
import os
import re
import subprocess
import time
from collections import Counter
from datetime import datetime
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import google.genai as genai
from report import generate as generate_report
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODEL   = os.environ.get("GEMINI_MODEL", "models/gemini-3.1-flash-lite-preview")

_client = genai.Client(api_key=GEMINI_API_KEY) if GEMINI_API_KEY else None

# ── Run queue (in-memory) ──────────────────────────────────────────────────────
_pending_run     = None   # dict {category, id} or None
_last_heartbeat  = 0.0    # epoch time of last daemon poll
_run_output      = ""     # captured stdout from last run
_run_status      = "idle" # "idle" | "running" | "done"
_run_started     = 0.0    # epoch time when run started
_run_report_html = ""     # full HTML of latest report (posted by daemon)
_run_results     = []     # list of result dicts from results_*.json
_run_result_stem = ""     # stem for latest posted results, e.g. 20260506_001311
_stop_requested  = False  # True when the user clicks Stop
_run_progress    = {}     # dict {current_test, current_category, completed, total}

PORT      = int(os.environ.get("PORT", 8765))
GH_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GH_REPO   = os.environ.get("GITHUB_REPO", "")
GH_FILE   = os.environ.get("GITHUB_FILE_PATH", "test_cases.py")

# Fallback: read/write local file if GitHub not configured (local dev)
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cases.py")
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
OVERALL_ANALYSIS_FILE = os.path.join(REPORTS_DIR, "overall_analysis.json")
USE_GITHUB = bool(GH_TOKEN and GH_REPO)

CATEGORIES = [
    "sticky_message","call_dedup","call_summary","language_pref",
    "location_memory","onboarding","capability","threep_nudge","interactive","generated",
]
TYPES = ["single","burst","sequence","dedup","burst_with_setup","interactive"]


# ── GitHub API helpers ─────────────────────────────────────────────────────────

def _gh_request(method: str, path: str, body: dict = None):
    url = f"https://api.github.com/repos/{GH_REPO}/contents/{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {GH_TOKEN}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"GitHub API {method} {path}: {e.code} {e.read().decode()}")


def _gh_get_file():
    """Returns (content_str, sha)."""
    data = _gh_request("GET", GH_FILE)
    content = base64.b64decode(data["content"]).decode()
    return content, data["sha"]


def _gh_put_file(content: str, sha: str, message: str = "Update test cases via eval UI"):
    encoded = base64.b64encode(content.encode()).decode()
    _gh_request("PUT", GH_FILE, {
        "message": message,
        "content": encoded,
        "sha": sha,
    })


def _gh_upsert_text_file(path: str, content: str, message: str):
    """Create or update a text file in the repo via GitHub Contents API."""
    body = {
        "message": message,
        "content": base64.b64encode(content.encode()).decode(),
    }
    try:
        existing = _gh_request("GET", path)
        body["sha"] = existing["sha"]
    except Exception:
        pass
    _gh_request("PUT", path, body)


def _write_text(path: str, content: str):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp_path, path)


def _write_json(path: str, payload):
    _write_text(path, json.dumps(payload, indent=2, default=str))


def _et_now_str(fmt: str) -> str:
    return datetime.now(ZoneInfo("America/New_York")).strftime(fmt)


# ── Test case load/save ────────────────────────────────────────────────────────

def load_test_cases():
    if USE_GITHUB:
        src, _ = _gh_get_file()
    else:
        with open(LOCAL_FILE) as f:
            src = f.read()
    match = re.search(r'TEST_CASES\s*=\s*(\[.*\])', src, re.DOTALL)
    if not match:
        return []
    return ast.literal_eval(match.group(1))


def save_test_cases(cases: list):
    new_list = "TEST_CASES = " + _format_list(cases)

    if USE_GITHUB:
        src, sha = _gh_get_file()
        src = re.sub(r'TEST_CASES\s*=\s*\[.*\]', new_list, src, flags=re.DOTALL)
        _gh_put_file(src, sha)
    else:
        with open(LOCAL_FILE) as f:
            src = f.read()
        src = re.sub(r'TEST_CASES\s*=\s*\[.*\]', new_list, src, flags=re.DOTALL)
        with open(LOCAL_FILE, "w") as f:
            f.write(src)


def _result_summary(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for r in results if (r.get("verdict") or "").upper() == "PASS")
    failed = sum(1 for r in results if (r.get("verdict") or "").upper() == "FAIL")
    unclear = total - passed - failed
    pass_rate = round((passed / total) * 100, 1) if total else 0.0
    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "unclear": unclear,
        "pass_rate": pass_rate,
    }


def _stem_sort_value(stem: str) -> int:
    """Sort run stems newest-first using the numeric timestamp embedded in the filename."""
    if not stem:
        return 0
    digits = "".join(ch for ch in stem if ch.isdigit())
    try:
        return int(digits)
    except Exception:
        return 0


def _build_overall_analysis_text(test_rows: list[dict], summary: dict) -> str:
    if not test_rows:
        return "No analysis yet. Run tests to populate cumulative insights."

    failed_rows = [t for t in test_rows if (t.get("verdict") or "").upper() == "FAIL"]
    unclear_rows = [t for t in test_rows if (t.get("verdict") or "").upper() == "UNCLEAR"]
    total = summary["total"]
    failed = summary["failed"]
    unclear = summary["unclear"]
    passed = summary["passed"]
    pass_rate = summary["pass_rate"]

    fail_test_counter = Counter((t.get("id") or "unknown") for t in failed_rows)
    fail_cat_counter = Counter((t.get("category") or "unknown") for t in failed_rows)
    fail_reason_counter = Counter(
        " ".join((t.get("reason") or "").split())[:140] for t in failed_rows if (t.get("reason") or "").strip()
    )

    top_fail_tests = ", ".join(f"{k} ({v})" for k, v in fail_test_counter.most_common(3)) or "none"
    top_fail_cats = ", ".join(f"{k} ({v})" for k, v in fail_cat_counter.most_common(3)) or "none"
    top_fail_reasons = "\n".join(
        f"- {reason} ({count})" for reason, count in fail_reason_counter.most_common(3)
    ) or "- none"

    lines = [
        f"OVERALL_SCORE: {passed}/{total} passed ({pass_rate}%)",
        (
            f"SUMMARY: Across all recorded runs, the system has {failed} failures and {unclear} unclear outcomes. "
            f"The current cumulative pass rate is {pass_rate}%."
        ),
        "HITS:",
        f"- Total passed outcomes so far: {passed}.",
        f"- Stable categories with fewer failures are visible where fail counts remain low outside the top failing categories.",
        "MISSES:",
        f"- Most repeated failing tests: {top_fail_tests}.",
        f"- Most affected categories: {top_fail_cats}.",
        "BEHAVIOR_PATTERN:",
        (
            "Recent failures cluster around repeated scenarios rather than random spread, which suggests a few persistent behavior gaps."
        ),
        "GAPS:",
        f"- Repeated failure reasons observed:\n{top_fail_reasons}",
        "RECOMMENDATION:",
        (
            "Prioritize fixes for the top failing tests/categories, then re-run only those categories to validate lift before full-suite regression."
        ),
    ]
    return "\n".join(lines)


def _build_analysis_payload() -> dict:
    files = sorted(
        glob.glob(os.path.join(REPORTS_DIR, "results_*.json")),
        key=lambda p: _stem_sort_value(os.path.basename(p).replace("results_", "").replace(".json", "")),
        reverse=True,
    )

    runs = []
    tests = []

    for f in files:
        stem = os.path.basename(f).replace("results_", "").replace(".json", "")
        try:
            with open(f) as fp:
                data = json.load(fp)
        except Exception:
            continue

        if not isinstance(data, list):
            continue

        summary = _result_summary(data)
        runs.append({
            "stem": stem,
            "ts": stem,
            **summary,
        })

        for r in data:
            tests.append({
                "stem": stem,
                "ts": stem,
                "id": r.get("id"),
                "name": r.get("name"),
                "category": r.get("category"),
                "verdict": (r.get("verdict") or "UNCLEAR").upper(),
                "reason": r.get("reason", ""),
                "tasks_sent_count": len(r.get("tasks_sent", []) or []),
                "responses_count": len(r.get("responses", []) or []),
            })

    if _run_results and _run_result_stem and not any(r["stem"] == _run_result_stem for r in runs):
        summary = _result_summary(_run_results)
        runs.insert(0, {
            "stem": _run_result_stem,
            "ts": _run_result_stem,
            **summary,
        })
        for r in _run_results:
            tests.insert(0, {
                "stem": _run_result_stem,
                "ts": _run_result_stem,
                "id": r.get("id"),
                "name": r.get("name"),
                "category": r.get("category"),
                "verdict": (r.get("verdict") or "UNCLEAR").upper(),
                "reason": r.get("reason", ""),
                "tasks_sent_count": len(r.get("tasks_sent", []) or []),
                "responses_count": len(r.get("responses", []) or []),
            })

    summary = _result_summary(tests)
    run_count = len(runs)
    latest_stem = runs[0]["stem"] if runs else ""

    overall_text = ""
    persisted = {}
    try:
        with open(OVERALL_ANALYSIS_FILE) as fp:
            persisted = json.load(fp)
    except Exception:
        persisted = {}

    if (
        persisted.get("source_run_count") == run_count
        and persisted.get("latest_stem") == latest_stem
        and isinstance(persisted.get("text"), str)
        and persisted.get("text", "").strip()
    ):
        overall_text = persisted["text"]
    else:
        overall_text = _build_overall_analysis_text(tests, summary)
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(OVERALL_ANALYSIS_FILE, "w") as fp:
            json.dump({
                "updated_at": _et_now_str("%Y-%m-%d %H:%M:%S ET"),
                "source_run_count": run_count,
                "latest_stem": latest_stem,
                "text": overall_text,
            }, fp, indent=2)

    return {
        "summary": summary,
        "overall_analysis": overall_text,
        "tests": tests,
        "runs": runs,
    }


def _persist_run_artifacts(stem: str, results: list[dict], report_html: str = ""):
    """Persist the latest run locally and sync it to GitHub when configured."""
    if not stem or not results:
        return

    os.makedirs(REPORTS_DIR, exist_ok=True)
    results_name = f"results_{stem}.json"
    report_name = f"report_{stem}.html"
    results_path = os.path.join(REPORTS_DIR, results_name)
    report_path = os.path.join(REPORTS_DIR, report_name)
    pointer_path = os.path.join(REPORTS_DIR, ".latest_results_path")

    _write_json(results_path, results)
    if report_html:
        _write_text(report_path, report_html)
    else:
        generate_report(results, output_path=report_path)
    _write_text(pointer_path, results_name)

    analysis = _build_analysis_payload()
    analysis_path = {
        "updated_at": _et_now_str("%Y-%m-%d %H:%M:%S ET"),
        "source_run_count": len(analysis.get("runs", [])),
        "latest_stem": analysis.get("runs", [{}])[0].get("stem", stem) if analysis.get("runs") else stem,
        "text": analysis.get("overall_analysis", ""),
    }
    _write_json(OVERALL_ANALYSIS_FILE, analysis_path)

    if USE_GITHUB:
        try:
            with open(report_path, encoding="utf-8") as f:
                report_content = f.read()
            with open(OVERALL_ANALYSIS_FILE, encoding="utf-8") as f:
                analysis_content = f.read()
            _gh_upsert_text_file(
                f"reports/{results_name}",
                json.dumps(results, indent=2, default=str),
                f"Sync eval results {stem}",
            )
            _gh_upsert_text_file(
                f"reports/{report_name}",
                report_content,
                f"Sync eval report {stem}",
            )
            _gh_upsert_text_file(
                "reports/.latest_results_path",
                results_name,
                f"Sync latest report pointer {stem}",
            )
            _gh_upsert_text_file(
                "reports/overall_analysis.json",
                analysis_content,
                f"Sync cumulative analysis {stem}",
            )
        except Exception as e:
            print(f"  [report sync error] {e}")

# ── Test case generation with LLM ─────────────────────────────────────────────

def generate_test_cases(prompt: str, count: int = 3) -> list:
    """Use Gemini to generate test cases based on a prompt."""
    
    generation_prompt = f"""
You are an expert QA engineer creating test cases for Asmi, an AI personal assistant that works via iMessage.

Asmi can:
- Research information online (restaurants, businesses, etc.)
- Make outbound phone calls to businesses on behalf of users
- Send emails and handle scheduling
- Remember user preferences and context across conversations

Generate {count} diverse test cases based on this prompt: "{prompt}"

Each test case should be a Python dict with these exact keys:
- 'id': unique identifier like 'gen_01', 'gen_02', etc.
- 'name': descriptive test name
- 'category': one of {CATEGORIES}
- 'type': one of {TYPES}
- 'message': the iMessage text to send to Asmi
- 'wait': seconds to wait (reasonable number based on task complexity)
- 'pass_criteria': specific, measurable success criteria
- 'expected_responses': number of responses expected (usually 1, or more for multi-part tasks)

Make test cases realistic and varied. Focus on edge cases and real user scenarios.
Return ONLY valid Python list of dicts, no other text.
"""

    if not _client:
        raise RuntimeError("GEMINI_API_KEY not configured")

    try:
        response = _client.models.generate_content(
            model=GEMINI_MODEL,
            contents=generation_prompt
        )
        generated_code = response.text.strip()
        
        # Clean up the response (remove markdown code blocks if present)
        if generated_code.startswith('```python'):
            generated_code = generated_code[9:]
        if generated_code.startswith('```'):
            generated_code = generated_code[3:]
        if generated_code.endswith('```'):
            generated_code = generated_code[:-3]
        
        generated_code = generated_code.strip()

        # Parse: try Python literal first, then JSON
        try:
            cases = ast.literal_eval(generated_code)
        except (ValueError, SyntaxError):
            cases = json.loads(generated_code)
        
        # Validate and fix the generated cases
        validated_cases = []
        for i, case in enumerate(cases):
            if not isinstance(case, dict):
                continue
                
            # Ensure required fields
            validated_case = {
                'id': case.get('id') or f'gen_{i+1:02d}',
                'name': case.get('name') or f'Generated test {i+1}',
                'category': case.get('category') if case.get('category') in CATEGORIES else 'capability',
                'type': case.get('type') if case.get('type') in TYPES else 'single',
                'message': case.get('message') or case.get('messages', ['Test message'])[0],
                'wait': int(case.get('wait') or 60),
                'pass_criteria': case.get('pass_criteria') or 'Test passes if Asmi responds appropriately',
                'expected_responses': int(case.get('expected_responses') or 1)
            }
            validated_cases.append(validated_case)
        
        return validated_cases[:count]  # Limit to requested count
        
    except Exception as e:
        # Return a fallback test case if generation fails
        return [{
            'id': 'gen_fallback',
            'name': 'Generated test case (fallback)',
            'category': 'capability',
            'type': 'single',
            'message': prompt,
            'wait': 60,
            'pass_criteria': 'Asmi responds to the generated prompt appropriately',
            'expected_responses': 1
        }]

def analyze_behavior(results: list) -> dict:
    """Call Gemini with all results to produce coherent behavior analysis."""
    if not _client:
        raise RuntimeError("GEMINI_API_KEY not configured")

    lines = []
    for r in results:
        lines.append(f"\n--- Test: {r.get('id')} | {r.get('name')} | Verdict: {r.get('verdict','?')} ---")
        for i, t in enumerate(r.get('tasks_sent', [])):
            lines.append(f"  Sent [{i+1}]: {t}")
        for i, rsp in enumerate(r.get('responses', [])):
            lines.append(f"  Response [{i+1}]: {rsp}")
        lines.append(f"  Judge: {r.get('reason','')}")

    prompt = f"""You are analyzing eval results for Asmi, an AI personal assistant accessible via iMessage.
Asmi handles: web research, outbound calls to businesses, emails, scheduling, memory of preferences.

Below are all the test results from a generated eval run. Analyze them TOGETHER as a coherent whole —
not just test by test. Look at how Asmi handled the scenario overall.

{chr(10).join(lines)}

Provide a structured behavior analysis in this EXACT format:

OVERALL_SCORE: X/Y passed
SUMMARY: 2-3 sentences on how Asmi is currently handling this scenario category overall.
HITS:
- (what Asmi is doing well, be specific with examples from responses)
- ...
MISSES:
- (what Asmi is failing at or doing suboptimally, specific examples)
- ...
BEHAVIOR_PATTERN: 1-2 sentences describing Asmi's current consistent behavior pattern for this type of request.
GAPS: Specific capability or behavior gaps observed.
RECOMMENDATION: What should be added/changed in Asmi's skills or memory to fix the misses.
"""
    result = _client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
    return {"text": result.text.strip()}


def _format_list(cases: list) -> str:
    lines = ["["]
    for tc in cases:
        lines.append("    {")
        for k, v in tc.items():
            lines.append(f"        {repr(k)}: {repr(v)},")
        lines.append("    },")
    lines.append("]")
    return "\n".join(lines)


# ── HTML ───────────────────────────────────────────────────────────────────────

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Asmi Testing</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #eef2f7; color: #1e293b; }
header { background: #ede9fe;
         color: #0f172a; padding: 12px 24px;
         display: flex; align-items: center; justify-content: center; gap: 12px;
         border-bottom: none; }
.brand-logo { width: 30px; height: 30px; border-radius: 6px; object-fit: cover; }
.header-text { display:flex; flex-direction:column; gap:2px; align-items:center; }
.header-title { font-size: 1.15rem; font-weight: 700; letter-spacing:-.01em; }
.toolbar { background: #ede9fe; border-bottom: 1px solid #d8b4fe;
           padding: 10px 24px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; justify-content: space-between; }
.toolbar-row { width: 100%; display:flex; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center; }
.toolbar-row.tabs { justify-content:center; }
.tab-btn { border-radius:12px; padding:6px 14px; font-size:0.92rem; font-weight:800; cursor:pointer; }
.tab-tests { background:#ffffff; border:1px solid #bfdbfe; color:#1d4ed8; }
.tab-reports { background:#ffffff; border:1px solid #fecaca; color:#b91c1c; }
.tab-responses { background:#ffffff; border:1px solid #bbf7d0; color:#166534; }
.tab-analysis { background:#ffffff; border:1px solid #e9d5ff; color:#7e22ce; }
.tab-tests.active { background:#eff6ff; border-color:#60a5fa; color:#1d4ed8; }
.tab-reports.active { background:#fff1f2; border-color:#f87171; color:#b91c1c; }
.tab-responses.active { background:#f0fdf4; border-color:#22c55e; color:#166534; }
.tab-analysis.active { background:#faf5ff; border-color:#a855f7; color:#7e22ce; }
.tab-menu { width:100%; display:none; gap:10px; align-items:center; flex-wrap:wrap; justify-content:center; }
.toolbar select, .toolbar input {
  height: 34px;
  padding: 0 12px;
  border: 1px solid #d8b4fe;
  border-radius: 10px;
  font-size: 0.85rem;
}
.toolbar input { min-width: 260px; max-width: 360px; width: 32vw; }
.toolbar input:focus { outline: none; border-color: #a855f7; box-shadow: 0 0 0 3px rgba(168,85,247,0.18); }
.toolbar input, .toolbar select { background:#ffffff; color:#0f172a; }
.toolbar input::placeholder { color:#64748b; }
.toolbar select { padding-right: 20px; }
.btn { padding: 5px 12px; border-radius: 6px; border: none; cursor: pointer;
       font-size: 0.8rem; font-weight: 600; }
.tab-menu .btn {
  height: 34px;
  padding: 0 12px;
  border-radius: 10px;
  display: inline-flex;
  align-items: center;
  line-height: 1;
}
.btn-primary { background: #3b82f6; color: white; }
.btn-primary:hover { background: #2563eb; }
.btn-success { background: #22c55e; color: white; }
.btn-success:hover { background: #16a34a; }
.btn-danger  { background: #ef4444; color: white; }
.btn-danger:hover  { background: #dc2626; }
.btn-outline { background: #ffffff; color: #0f172a; border: 1px solid #d8b4fe; }
.btn-outline:hover { background: #f8fafc; }
.count { background: #eff6ff; color: #1d4ed8; padding: 4px 10px;
         border-radius: 99px; font-size: 0.8rem; font-weight: 700; }
main { padding: 24px; max-width: 1300px; margin: 0 auto; }
/* Monday.com style table */
.test-table { width:100%; border-collapse:collapse; background:white;
              border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.06); }
.test-table th { padding:9px 12px; text-align:left; font-size:0.7rem; font-weight:700;
                  text-transform:uppercase; letter-spacing:.05em; color:#94a3b8;
                  background:#f8fafc; border-bottom:2px solid #e2e8f0; white-space:nowrap; }
.test-table td { padding:9px 12px; vertical-align:middle; border-bottom:1px solid #f1f5f9;
                  font-size:0.87rem; }
.cat-row td { background:#f8fafc; font-size:0.72rem; font-weight:700; text-transform:uppercase;
              letter-spacing:.06em; color:#64748b; padding:6px 12px;
              border-top:2px solid #e2e8f0; border-bottom:1px solid #e2e8f0; }
.test-row:hover td { background:#fafbff; cursor:pointer; }
.test-row.editing > td { background:#eff6ff !important; }
.edit-row > td { padding:0; background:white !important; }
.edit-form-inner { padding:18px; border-top:3px solid #3b82f6; }
.result-row > td { padding:0; }
.cat-pill { display:inline-block; padding:2px 8px; border-radius:99px;
            font-size:0.69rem; font-weight:700; white-space:nowrap; }
.type-pill { display:inline-block; padding:2px 7px; border-radius:4px; font-size:0.7rem;
             font-weight:600; background:#f0fdf4; color:#15803d; white-space:nowrap; }
.msg-cell { max-width:300px; overflow:hidden; text-overflow:ellipsis;
            white-space:nowrap; color:#475569; font-size:0.82rem; }
.id-cell { font-family:monospace; font-size:0.78rem; color:#94a3b8; white-space:nowrap; }
.run-cell-btn { background:#7c3aed; color:white; border:none; border-radius:5px;
                padding:4px 9px; font-size:0.8rem; font-weight:700; cursor:pointer; }
.run-cell-btn:hover { background:#6d28d9; }
.run-cell-btn:disabled { opacity:.5; cursor:not-allowed; }
.action-btn { background:none; border:none; cursor:pointer; font-size:0.9rem;
              padding:3px 5px; border-radius:4px; color:#64748b; }
.action-btn:hover { background:#f1f5f9; color:#1e293b; }
.drag-handle { cursor: grab; user-select: none; }
.drag-handle:active { cursor: grabbing; }
.test-table input[type=checkbox], .cat-row input[type=checkbox] {
    width:14px; height:14px; cursor:pointer; margin:0;
}
.badge { padding: 2px 8px; border-radius: 99px; font-size: 0.72rem; font-weight: 700; }
.badge-warn { background: #fffbeb; color: #b45309; }
.form-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.form-full { grid-column: 1 / -1; }
label { display: block; font-size: 0.78rem; font-weight: 600; color: #475569;
        text-transform: uppercase; letter-spacing: .04em; margin-bottom: 4px; }
input[type=text], textarea, select {
    width: 100%; padding: 8px 10px; border: 1px solid #e2e8f0;
    border-radius: 6px; font-size: 0.88rem; font-family: inherit;
    background: #fafafa; transition: border .15s; }
input[type=text]:focus, textarea:focus, select:focus {
    outline: none; border-color: #3b82f6; background: white; }
textarea { resize: vertical; min-height: 70px; }
.form-actions { margin-top: 16px; display: flex; gap: 8px; flex-wrap:wrap; }
.new-form { background: white; border-radius: 12px; padding: 20px;
            margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.06);
            display: none; border: 2px dashed #3b82f6; }
.new-form.open { display: block; }
.saving { opacity: 0.6; pointer-events: none; }
.btn-run { background: #7c3aed; color: white; }
.btn-run:hover { background: #6d28d9; }
#toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b; color: white;
         padding: 12px 20px; border-radius: 8px; font-size: 0.85rem; font-weight: 500;
         opacity: 0; transition: opacity .3s; pointer-events: none; z-index: 999; }
#toast.show { opacity: 1; }
#outputPanel { display:none; background:#0f172a; color:#cbd5e1; font-family:monospace;
               font-size:0.78rem; border-bottom:3px solid #3b82f6; }
#outputHeader { display:flex; align-items:center; gap:12px; padding:10px 24px;
                background:#1e293b; color:#94a3b8; font-size:0.8rem; }
#outputHeader strong { color:#e2e8f0; }
#outputBody { padding:0; }
#outputBodyText { padding:10px 24px 8px; white-space:pre-wrap; line-height:1.5;
                  font-family:monospace; font-size:0.78rem; color:#94a3b8; }
#resultsTable { display:none; background:white; padding:16px 24px 20px; }
.rt-summary { font-size:0.9rem; font-weight:600; color:#1e293b; margin-bottom:16px;
              display:flex; gap:16px; align-items:center; flex-wrap:wrap; }
.rt-pass { color:#16a34a; font-weight:700; }
.rt-fail { color:#dc2626; font-weight:700; }
.rt-unclear { color:#b45309; font-weight:700; }
.rt-pct  { font-size:1.6rem; font-weight:800; }
/* Rich result cards */
.rt-card { border-radius:8px; margin-bottom:10px; overflow:hidden;
           border:1px solid #e2e8f0; }
.rt-card.rt-pass  { border-left:4px solid #22c55e; }
.rt-card.rt-fail  { border-left:4px solid #ef4444; }
.rt-card.rt-unclear { border-left:4px solid #f59e0b; }
.rt-card-hdr { display:flex; align-items:center; gap:10px; padding:10px 14px;
               background:#f8fafc; flex-wrap:wrap; }
.rt-tid  { font-family:monospace; font-size:0.78rem; color:#94a3b8; flex-shrink:0; }
.rt-tname { font-weight:600; font-size:0.9rem; flex:1; }
.rt-badge { font-size:0.75rem; font-weight:700; padding:2px 8px; border-radius:99px; }
.rt-badge.rt-pass   { background:#dcfce7; color:#16a34a; }
.rt-badge.rt-fail   { background:#fee2e2; color:#dc2626; }
.rt-badge.rt-unclear{ background:#fef3c7; color:#b45309; }
.rt-dur { font-size:0.75rem; color:#94a3b8; margin-left:auto; flex-shrink:0; }
.rt-section { padding:10px 14px; border-top:1px solid #f1f5f9; }
.rt-slabel { font-size:0.68rem; font-weight:700; text-transform:uppercase;
             letter-spacing:.06em; color:#94a3b8; margin-bottom:6px; }
.rt-msg  { font-size:0.85rem; color:#374151; padding:7px 10px; background:#f8fafc;
           border-radius:5px; margin-bottom:4px; border:1px solid #f1f5f9; }
.rt-turn { background:#ffffff; border:1px solid #e2e8f0; border-radius:8px; padding:10px 12px; margin-bottom:8px; }
.rt-turn-head { font-weight:700; color:#334155; margin-bottom:6px; font-size:0.8rem; }
.rt-user { border-left:3px solid #3b82f6 !important; background:#eef2ff !important; }
.rt-resp-num { font-size:0.7rem; font-weight:700; color:#64748b; margin-right:6px; }
.rt-judge { font-size:0.85rem; color:#64748b; line-height:1.55; }
/* Inline result inside test card */
.inline-result { display:none; margin-top:14px; border-radius:8px; overflow:hidden;
                 border:1px solid #e2e8f0; font-size:0.85rem; }
.inline-result.running { display:block; padding:12px 14px; color:#7c3aed;
                          font-weight:600; background:#faf5ff; border-color:#e9d5ff; }
.inline-result.done { display:block; }
.inline-running-dots::after { content:'...'; animation:dots 1.2s steps(4,end) infinite; }
@keyframes dots { 0%,20%{content:'.'} 40%{content:'..'} 60%,100%{content:'...'} }
</style>
</head>
<body>

<header>
  <img class="brand-logo" src="data:image/jpeg;base64,/9j/4AAQSkZJRgABAQAAAQABAAD/2wCEAAkGBxAHEBAQEBEQDxANEA8QDxURDw8VDxMRFhEYGBURExcZKCggGBonGxUTIT0tJioxLzIuFx8zODMsNygtLisBCgoKDg0OGxAQGislICAsLS0tKystKy0tLS02KystLS0tKy8rNS4tLTctKzAtNS8tLSs4NS0tKy0tLTcrNy0tK//AABEIAOEA4QMBIgACEQEDEQH/xAAbAAEAAgMBAQAAAAAAAAAAAAAABQcBAgYDBP/EADwQAAIBAwAFBwkGBwEAAAAAAAABAgMEEQUGEiExBzRBYXFzsRMiMlFygZGysyQzUqHBwhQjQmJ00fBT/8QAGQEBAAMBAQAAAAAAAAAAAAAAAAECBAMF/8QAJhEBAAIBAwMDBQEAAAAAAAAAAAECEQMEMRIyQSFx8BMUMzRRBf/aAAwDAQACEQMRAD8Ao0AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAABtgwycDAAIAAAAZwGsE4GAAQAAAAzgzgnA1AMkDANmsGGBgAAAAAAAAAAAABvSpuq1GKy5NJJcW28JFi6C1Ro2cVKvGNaq1vUt9OPUlwfaznNQrRXF1tPeqEJTXtPzV4t+46fXbSs9G0FGm9mdeTipLjGKWZNde9L3m/badK0nVvGcKz/ABNbVKj5uaUPUswX5HyaR0HbaTi9unHL4TglGfblcfeVNOTk8t5b4t8TptSNLzt68aDk3SrZik3ujPGU4+rhj3nSm7re3RavpJhFae0RPQ9V05edFrapyxulHPiRhZOv1oq9p5T+qhOLT6pPZa/NfArYybnS+nqYjhMSHcas6owqQjWuU3t4lCnwSXQ59Oer/lyGjqSr1qUJejOpTjLf0OSTLc0lcfwdGrUSy6VOc0ujKi2kddnpVtm1uIRMsU6FCyWIxpUl1KEUKttQv1iUKVWPWoSx2PoKiu7qpdyc6kpTk28uTz8PUjawvqmj5qpTk4yi+jg+qS6UdPvq5x0+h0uk1r1WVhF16GfJL04PLcP7k+mPh4cmWW9arG6p7NSpjykMVI+TqvGY4lHKRW1VKLaTyk2k/WuhnDc104tmk8pjLQmNXNCS01U2c7NOG+pLpS6IrrZDlnajWqt7OEumtKU5fHZX5JfErttL6l8TwTKRstF2+jI+ZTpwwt8mltPtk957ShRvE4tUqq6ViEkV7rrpKd3czp7T8nRajGOdzljfJ+t58CCtLqdpJTpycJxeU1/29Gu27pS3TFfSEYd1pLUmnWqwlRfkqcn/ADY8cLjmnn4b/WT1jom20ZHzKcI4W+UknN9snvPo0fc/xtKnVW7ysIzx6m1vRXuu+kp3VxOltPydBqKinucsb5P1vLx7jrqfS0a9cRyj1lYMoUbxOLVKqulYhJe9HHa2arwtoOvbrEY76kOKS/HHq6jkbW5nazjOnJwnF5TX/b0W7YV1pKhTm0sVqacl0b15y8SlL03MTWYxKeFOswe99Q/halSn/wCc5w+Dxk8Dy5jCzt9QbGjd0qzqUqdRxqRSc6cZNLZ4LJH6+2tO0r0lThCmnRTahGMU3ty3tImOTj7mv3kflI3lG5xS7hfUkb7Vj7WJx8yr5ckADz1gAAAAB2nJsvOuH/bSX5y/0b8pL323ZV/aa8m3pXPs0vGRnlJ423ZV/aejH6nz+q+UFYavVb2n5ROMU/QUs5l/o8tB03RvKEZbpRrxi16mpYaPq0brJKypKm6ansZUXtYwvU1jefNoWs7i9ozl6U68ZPtcjDTuhSnXmerjwsPWpbVlcZ/Bn4STKoLY1o5ncd3+qKnNm/8AyR7L14bUaroyjOPGElJdqeUW7o2/paYoqccSjNYnF/0trfCSKfPrsL+ro+W3Sm4S6ccGvU1wZx2+v9KZzxKZjLtNI6i06rbo1HTzv2ZLaiupPivzIC81Qu7XLUFVSz93LLx2PDJGy17qQwqtKM+uEnF9uN6b+BP6P1stL1qO06UnuSqLCz7SyjT0bbU4nEo9YVlUpuk3GScWuKaaku1Gpa+sGhKemKbWEqqX8ufTnoi30xZVMouLw9zTafaZdfQnSnHiUxOWEW1qysWdv3USpUW1q1zO37qJ3/z++fZFlaaff2q476p8zI8+/T/OrjvqnzM+Ax37pWWzqvzO37v9WVrp5/arr/Ir/UZZWq3M7fu/1ZWunudXX+RX+ozbuvxU+eFY5fCWrqjzK39mXzyKqLV1R5lb+zL6kiuw759i3Cu9ZN13c99PxI0ktZOd3PfT8SNMd+6Vnf8AJx9zX7yPykbyjc4pdwvqSJLk4+5r95H5SN5RucUu4X1JHoW/Uj55V8uSAB5qwAAAAA7Xk29K59ml4yM8pPG27Kv7THJqvOufZpeMjblJW+27Kv7T0Y/U+f1Xy4kkNXed2/ew8SPJDV3ndv3sPEwU7oWWRrRzO47v9UVOWzrQvsdx3f6oqZm3f/kj2Vq3t1FyjttqO0ttrjs53te7J3VfUaioSdOpVc9luG04bLljdndwOBO/1R1lhUhGhXkoThiNOUn5s49Cb6H4nLa/Tmem/nhMuDqU3TbUk04tpp8U1xTFOm6jUYptyaSS4tvgkW5e6Htr97VWlCb/ABYxJrraxkzZaIt7B7VKlCEvxYzJdknvR2+wtnn0R1PbR1F29GlCTzKFOEZPrUUmVBfVFVq1JLhKpOS7HJtHf616y07SE6NKSlVmnFuO9U09zbf4sFdFd7qVnFI8FRFtatczt+6iVIW5q0vsdt3USdh3z7FlZaf51cd9U+ZnwH36f51cd9U+ZnwGK/dKzudC6329hb0qU41nKnHD2Y03Hj0ZkjkNJ3Cuq9apHOzVq1JxzxxKbaz17zq9XrWhO3T2acm8+VclFtPL3PPBHIXijGpUUPQU5qHs7Tx+R01NW16xWfDlTUi1pjHDyLV1R5lb+zL6kiqi1dUV9it/Zl88jRsPyT7L2V3rJzu576fiRpJ6y7ry576fiRpjv3Ss77k4+5r95H5SN5RucUu4X1JElybr+TX7yPykbyjL7RS7hfUkehb9SPnlXy5IAHmrAAAAAD0p1ZU/Rk4544bRipVlU9KTljhlt+JoCcjIi3F5W5rhjiYBA9ZXE5LDnJp8U5No8gCcgZyYBA+y30nXtt0K1WCXBKcsfAV9J17jdOtVkn0OpLHwPjBbrtxkZMAFRk9I3E4rCnJJcEpPB5ADMntb3vb+JgADdTazhvfx38e00AAyekbicFhTkkuhSaR5AnI2lJy3ve3xzxMGAQPSnWlT9GUo9kmjFSrKp6Tcu1ts0BOQABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAD//2Q==" alt="Asmi logo">
  <div class="header-text">
    <div class="header-title">Asmi Testing</div>
  </div>
</header>

<div id="outputPanel">
  <div id="outputHeader">
    <span id="outputStatus">Running…</span>
    <span id="outputElapsed" style="margin-left:auto;color:#64748b"></span>
    <button id="stopBtn" onclick="stopRun()" style="background:#dc2626;color:white;border:none;
            border-radius:4px;padding:3px 12px;cursor:pointer;font-size:0.75rem;font-weight:600;display:none">⏹ Stop</button>
    <button onclick="clearOutput()" style="background:#334155;color:#94a3b8;border:none;
            border-radius:4px;padding:3px 10px;cursor:pointer;font-size:0.75rem;margin-left:6px">✕ Close</button>
  </div>
  <div id="progressBar" style="display:none;padding:8px 24px 10px;background:#1e293b;border-top:1px solid #334155">
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:5px">
      <span id="progressLabel" style="font-size:0.75rem;color:#c4b5fd;font-family:monospace;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
      <span id="progressPct" style="font-size:0.75rem;color:#94a3b8;flex-shrink:0"></span>
    </div>
    <div style="background:#334155;border-radius:99px;height:5px;overflow:hidden">
      <div id="progressFill" style="background:#7c3aed;height:100%;width:0%;transition:width .4s ease;border-radius:99px"></div>
    </div>
  </div>
  <div id="outputBodyText"></div>
  <div id="resultsTable"></div>
  <div id="behaviorAnalysisPanel" style="display:none;padding:16px 24px 20px;border-top:1px solid #1e293b;">
    <div style="font-weight:700;font-size:0.9rem;color:#c4b5fd;margin-bottom:10px;">🧠 Asmi Behavior Analysis</div>
    <div id="behaviorAnalysisBox" style="background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px;font-family:monospace;font-size:0.78rem;white-space:pre-wrap;line-height:1.6;max-height:420px;overflow-y:auto;border:1px solid #1e293b;"></div>
    <div style="margin-top:10px;">
      <button onclick="saveBehaviorFromRun()" style="background:#22c55e;color:white;border:none;border-radius:6px;padding:7px 16px;font-size:0.82rem;font-weight:600;cursor:pointer;">💾 Save to ASMI_BEHAVIOR_ANALYSIS.md</button>
    </div>
  </div>
</div>
<div class="toolbar">
  <div class="toolbar-row tabs">
    <button class="tab-btn tab-tests" id="tabMain" onclick="showTab('main')">Tests</button>
    <button class="tab-btn tab-reports" id="tabHistory" onclick="showTab('history')">Reports</button>
    <button class="tab-btn tab-responses" id="tabResponses" onclick="showTab('responses')">Responses</button>
    <button class="tab-btn tab-analysis" id="tabAnalysis" onclick="showTab('analysis')">Analysis</button>
  </div>

  <div class="tab-menu" id="menuMain">
    <button class="btn btn-outline" onclick="toggleGenerate()" style="font-size:0.75rem;padding:3px 8px;background:#f3f4f6;color:#374151;border:1px solid #d1d5db;white-space:nowrap">Generate Test Cases with AI</button>
    <button class="btn btn-primary" onclick="toggleNew()" style="font-size:0.75rem;padding:3px 8px;white-space:nowrap">+ Add a test case</button>
    <input type="text" id="search" placeholder="Search tests…" oninput="filter()">
    <button class="btn btn-outline" id="collapseAllBtn" onclick="collapseAll()" style="font-size:0.75rem;padding:3px 8px;white-space:nowrap">Collapse all</button>
    <button class="btn btn-outline" id="expandAllBtn" onclick="expandAll()" style="font-size:0.75rem;padding:3px 8px;white-space:nowrap">Expand all</button>
    <button class="btn btn-outline" id="interactiveAutoBtn" onclick="toggleInteractiveAutoContinue()" style="font-size:0.75rem;padding:3px 8px;white-space:nowrap">Auto-continue: On</button>
    <span id="selectedBadge" class="count" style="background:#fef3c7;color:#92400e;">0 selected</span>
    <button class="btn btn-run" id="runBtn" onclick="runSelected()" style="font-size:0.75rem;padding:3px 8px;white-space:nowrap">▶ Run selected</button>
  </div>

  <div class="tab-menu" id="menuHistory">
    <span style="font-size:0.82rem;color:#0f172a;">View run history and download report files.</span>
  </div>

  <div class="tab-menu" id="menuResponses">
    <span style="font-size:0.82rem;color:#0f172a;">Review sent messages and captured replies by run.</span>
  </div>

  <div class="tab-menu" id="menuAnalysis">
    <span style="font-size:0.82rem;color:#0f172a;">See cumulative verdict trends and per-test reasoning.</span>
  </div>
</div>

<main>

  <!-- New test form -->
  <div class="new-form" id="newForm">
    <div style="font-weight:700;margin-bottom:14px;font-size:1rem;">New Test Case</div>
    <div class="form-grid">
      <div><label>Name</label><input type="text" id="new_name" placeholder="Short description"></div>
      <div>
        <label>Category</label>
        <div style="display:flex;gap:6px;">
          <select id="new_category" onchange="_handleCategoryChange()" style="flex:1;"></select>
          <input type="text" id="new_cat_input" placeholder="New category" style="flex:1;display:none;">
          <button type="button" id="new_cat_btn" class="btn btn-primary" style="display:none;padding:6px 12px;font-size:0.8rem;" onclick="_confirmNewCategory()">Create</button>
        </div>
      </div>
      <div>
        <label>Type</label>
        <select id="new_type" onchange="toggleNewMsgFields()">
          <option value="single">single</option>
          <option value="burst">burst</option>
          <option value="sequence">sequence</option>
          <option value="dedup">dedup</option>
          <option value="burst_with_setup">burst_with_setup</option>
          <option value="interactive">interactive</option>
        </select>
      </div>
      <div class="form-full" id="new_msg_wrap">
        <label>Message</label>
        <input type="text" id="new_message" placeholder="Exact iMessage to send to Asmi">
      </div>
      <div class="form-full" id="new_msgs_wrap" style="display:none">
        <label>Messages (one per line)</label>
        <textarea id="new_messages" placeholder="Message 1&#10;Message 2&#10;Message 3"></textarea>
      </div>
      <div class="form-full" id="new_interactive_wrap" style="display:none">
        <label>Follow-up Replies (one per line)</label>
        <textarea id="new_followups" placeholder="Reply 1&#10;Reply 2&#10;Reply 3"></textarea>
        <div style="display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-top:10px;">
          <div>
            <label>Auto continue</label>
            <select id="new_auto_continue">
              <option value="true" selected>On</option>
              <option value="false">Off</option>
            </select>
          </div>
          <div>
            <label>Max turns</label>
            <input type="text" id="new_max_turns" placeholder="3">
          </div>
          <div>
            <label>Stop when</label>
            <input type="text" id="new_stop_when" placeholder="comma or line separated phrases">
          </div>
        </div>
      </div>
      <div id="new_wait_wrap" style="display:none">
        <label>Response Speed</label>
        <select id="new_wait_preset">
          <option value="60">Fast — simple reply expected (60s)</option>
          <option value="120" selected>Normal — default (120s)</option>
          <option value="180">Slow — Asmi needs to make a call (180s)</option>
          <option value="300">Very Slow — complex task or research (300s)</option>
        </select>
      </div>
      <div class="form-full"><label>Pass Criteria</label>
        <textarea id="new_pass_criteria" placeholder="What does a passing response look like? Be specific."></textarea>
      </div>
      <div class="form-full"><label>Precondition (optional)</label>
        <input type="text" id="new_precondition" placeholder="e.g. Fresh account required">
      </div>
      <div class="form-full"><label>Manual Check (optional)</label>
        <input type="text" id="new_manual_check" placeholder="e.g. Check ElevenLabs dashboard">
      </div>
    </div>
    <div class="form-actions">
      <button class="btn btn-primary" onclick="addNew()">Add Test</button>
      <button class="btn btn-outline" onclick="toggleNew()">Cancel</button>
    </div>
  </div>

  <!-- Generate test cases form -->
  <div class="new-form" id="generateForm" style="border-color:#7c3aed;">
    <div style="font-weight:700;margin-bottom:14px;font-size:1rem;color:#7c3aed;">🤖 Generate Test Cases with AI</div>
    <div style="margin-bottom:16px;font-size:0.9rem;color:#64748b;">
      Describe what you want to test, and the AI will generate multiple test cases for you.
      Access to entire codebase and Asmi's capabilities.
    </div>
    <div class="form-grid">
      <div class="form-full">
        <label>Test Scenario Description</label>
        <textarea id="generate_prompt" placeholder="e.g. Test Asmi's ability to handle complex restaurant reservations with multiple requirements, dietary restrictions, and follow-up questions" rows="4"></textarea>
      </div>
      <div>
        <label>Number of Tests</label>
        <select id="generate_count">
          <option value="1">1 test case</option>
          <option value="2">2 test cases</option>
          <option value="3" selected>3 test cases</option>
          <option value="5">5 test cases</option>
        </select>
      </div>
      <div>
        <label>Category Focus (optional)</label>
        <select id="generate_category" style="flex:1;"><option value="">Any category</option></select>
      </div>
    </div>
    <div class="form-actions">
      <button class="btn" style="background:#7c3aed;color:white;" onclick="generateTests()" id="generateBtn">
        <span id="generateBtnText">🤖 Generate Tests</span>
      </button>
      <button class="btn btn-outline" onclick="toggleGenerate()">Cancel</button>
    </div>
    <div id="generatedPreview" style="margin-top:16px;display:none;">
      <div style="font-weight:600;margin-bottom:8px;color:#374151;">Generated Test Cases:</div>
      <div id="generatedList" style="border:1px solid #e2e8f0;border-radius:6px;padding:12px;background:#f8fafc;"></div>
      <div style="margin-top:12px;display:flex;gap:8px;flex-wrap:wrap;align-items:center;">
        <button class="btn" style="background:#7c3aed;color:white;" onclick="saveAndRunGenerated()" id="runGenBtn">▶ Save to Generated &amp; Run All</button>
        <button class="btn btn-outline" onclick="clearGenerated()">Clear</button>
        <span id="genRunStatus" style="color:#64748b;font-size:0.83rem;margin-left:4px;"></span>
      </div>
      <div id="genAnalysisSection" style="display:none;margin-top:20px;">
        <div style="font-weight:700;font-size:0.95rem;color:#1e293b;margin-bottom:8px;">🧠 Overall Behavior Analysis</div>
        <div id="genAnalysisBox" style="background:#1e293b;color:#e2e8f0;border-radius:8px;padding:16px;font-family:monospace;font-size:0.82rem;white-space:pre-wrap;line-height:1.6;max-height:400px;overflow-y:auto;"></div>
        <div style="margin-top:10px;display:flex;gap:8px;">
          <button class="btn btn-success" onclick="saveBehaviorAnalysis()">💾 Save to ASMI_BEHAVIOR_ANALYSIS.md</button>
        </div>
      </div>
    </div>
  </div>

  <div id="mainSection">
    <div id="testList"></div>
  </div>

  <div id="historySection" style="display:none; padding:16px;">
    <h2 style="margin-bottom:16px;">Reports</h2>
    <div id="historyList" style="background:white; border-radius:12px; overflow:hidden; box-shadow:0 1px 4px rgba(0,0,0,.06);">
      <p style="color:#94a3b8;padding:20px;text-align:center;">Loading…</p>
    </div>
  </div>

  <div id="responsesSection" style="display:none; padding:16px;">
    <h2 style="margin-bottom:16px;">Responses</h2>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;">
      <button class="btn btn-outline" id="responsesViewTranscriptBtn" onclick="setResponsesView('transcript')" style="font-size:0.75rem;padding:4px 10px;white-space:nowrap">Transcript</button>
      <button class="btn btn-outline" id="responsesViewHistoryBtn" onclick="setResponsesView('history')" style="font-size:0.75rem;padding:4px 10px;white-space:nowrap">All conversations</button>
    </div>
    <div id="responsesList" style="display:grid;gap:16px;"></div>
  </div>

  <div id="analysisSection" style="display:none; padding:16px;">
    <h2 style="margin-bottom:16px;">Analysis</h2>
    <div id="analysisSummary" style="display:grid;gap:12px;"></div>
    <div id="analysisList" style="margin-top:16px;display:grid;gap:12px;"></div>
  </div>
</main>
<div id="toast"></div>
<script>
let tests = [];
let _editingId = null;
let collapsedCats = new Set();
let _sortBy = 'default';
let _categoryOrder = [];
let _responsesViewMode = 'transcript';
let _dragState = null;

const CAT_META = {
  onboarding:         {label:'Onboarding',        color:'#7c3aed', bg:'#f5f3ff'},
  capability:         {label:'Capability',         color:'#1d4ed8', bg:'#eff6ff'},
  sticky_message:     {label:'Sticky Message',     color:'#15803d', bg:'#f0fdf4'},
  call_dedup:         {label:'Call Dedup',         color:'#c2410c', bg:'#fff7ed'},
  cadence_control:    {label:'Cadence Control',    color:'#dc2626', bg:'#fef2f2'},
  call_summary:       {label:'Call Summary',       color:'#0e7490', bg:'#ecfeff'},
  voicemail:          {label:'Voicemail',          color:'#475569', bg:'#f1f5f9'},
  task_reliability:   {label:'Task Reliability',   color:'#b45309', bg:'#fffbeb'},
  task_specific_call: {label:'Task Calls',         color:'#7c2d12', bg:'#fff7ed'},
  threep_nudge:       {label:'3P Nudge',           color:'#6d28d9', bg:'#f5f3ff'},
  location_memory:    {label:'Location Memory',    color:'#065f46', bg:'#ecfdf5'},
  language_pref:      {label:'Language',           color:'#0369a1', bg:'#f0f9ff'},
  timezone:           {label:'Timezone',           color:'#1e40af', bg:'#dbeafe'},
  checklist:          {label:'Checklist',          color:'#166534', bg:'#dcfce7'},
  chat_brevity:       {label:'Chat Brevity',       color:'#9d174d', bg:'#fdf2f8'},
  chat_flow:          {label:'Chat Flow',          color:'#831843', bg:'#fce7f3'},
  interactive:        {label:'Interactive',        color:'#4c1d95', bg:'#f5f3ff'},
  personalization:    {label:'Personalization',    color:'#3730a3', bg:'#eef2ff'},
  reengagement:       {label:'Reengagement',       color:'#7e22ce', bg:'#faf5ff'},
  guardrails:         {label:'Guardrails',         color:'#991b1b', bg:'#fee2e2'},
  generated:          {label:'Generated',          color:'#334155', bg:'#f1f5f9'},
};
const CAT_ORDER = [
  'onboarding','capability','sticky_message','call_dedup','cadence_control',
  'call_summary','voicemail','task_reliability','task_specific_call','threep_nudge',
  'location_memory','language_pref','timezone','checklist','chat_brevity','chat_flow',
  'interactive',
  'personalization','reengagement','guardrails','generated',
];

function _catOptions(selected = '', includeAll = false, includeAdd = true) {
  let html = includeAll ? '<option value="">All categories</option>' : '';
  Array.from(new Set(CAT_ORDER)).forEach(c => {
    const m = CAT_META[c] || {label:c};
    html += `<option value="${c}" ${selected===c?'selected':''}>${m.label}</option>`;
  });
  if (!includeAll && includeAdd) html += '<option value="__add__">+ Add Category</option>';
  return html;
}

function _handleCategoryChange() {
  const el = document.getElementById('new_category');
  const input = document.getElementById('new_cat_input');
  const btn = document.getElementById('new_cat_btn');

  if (el.value === '__add__') {
    el.style.display = 'none';
    input.style.display = '';
    btn.style.display = '';
    input.value = '';
    input.focus();
  }
}

function _confirmNewCategory() {
  const input = document.getElementById('new_cat_input');
  const name = input.value.trim();
  if (!name) { alert('Enter a category name'); return; }
  if (CAT_ORDER.includes(name)) {
    alert('Category already exists');
    return;
  }

  const el = document.getElementById('new_category');
  const btn = document.getElementById('new_cat_btn');

  CAT_ORDER.push(name);
  CAT_META[name] = {label: name.replace(/_/g, ' ').split(' ').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' '), color: '#64748b', bg: '#f1f5f9'};
  _initCatDropdowns();

  el.style.display = '';
  input.style.display = 'none';
  btn.style.display = 'none';
  el.value = name;
}

function _dedupeSelectOptions(selectId) {
  const el = document.getElementById(selectId);
  if (!el) return;
  const seen = new Set();
  Array.from(el.options).forEach(opt => {
    const key = `${opt.value}||${opt.text}`;
    if (seen.has(key)) opt.remove();
    else seen.add(key);
  });
}

function _categoryOrderFromTests(list = tests) {
  const order = [];
  const seen = new Set();
  list.forEach(t => {
    if (!seen.has(t.category)) {
      seen.add(t.category);
      order.push(t.category);
    }
  });
  return order;
}

function _saveCurrentOrder() {
  return saveAll();
}

function _moveItemInArray(arr, fromIdx, toIdx) {
  if (fromIdx === toIdx || fromIdx < 0 || toIdx < 0 || fromIdx >= arr.length || toIdx >= arr.length) return arr;
  const copy = arr.slice();
  const [item] = copy.splice(fromIdx, 1);
  copy.splice(toIdx, 0, item);
  return copy;
}

function _reorderTestsByCategory(sourceCat, targetCat, insertBefore = true) {
  if (!sourceCat || !targetCat || sourceCat === targetCat) return;
  const source = tests.filter(t => t.category === sourceCat);
  const target = tests.filter(t => t.category === targetCat);
  if (!source.length || !target.length) return;
  const remaining = tests.filter(t => t.category !== sourceCat);
  const targetIndex = remaining.findIndex(t => t.category === targetCat);
  if (targetIndex < 0) return;
  const insertAt = insertBefore ? targetIndex : targetIndex + target.length;
  remaining.splice(insertAt, 0, ...source);
  tests = remaining;
  render();
  _saveCurrentOrder();
  toast(`Moved ${sourceCat} category`);
}

function _reorderTestWithinCategory(sourceId, targetId, insertBefore = true) {
  if (!sourceId || !targetId || sourceId === targetId) return;
  const source = tests.find(t => t.id === sourceId);
  const target = tests.find(t => t.id === targetId);
  if (!source || !target || source.category !== target.category) return;
  const sourceIdx = tests.findIndex(t => t.id === sourceId);
  const targetIdx = tests.findIndex(t => t.id === targetId);
  if (sourceIdx < 0 || targetIdx < 0) return;
  const next = tests.slice();
  const [item] = next.splice(sourceIdx, 1);
  const adjustedTargetIdx = sourceIdx < targetIdx ? targetIdx - 1 : targetIdx;
  next.splice(insertBefore ? adjustedTargetIdx : adjustedTargetIdx + 1, 0, item);
  tests = next;
  render();
  _saveCurrentOrder();
  toast(`Moved ${sourceId}`);
}

function dragStartCategory(ev, cat) {
  _dragState = {type: 'category', id: cat};
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', `category:${cat}`);
}

function endDrag() {
  _dragState = null;
}

function dragOverCategory(ev) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
}

function dropCategory(ev, cat) {
  ev.preventDefault();
  const src = _dragState;
  _dragState = null;
  if (!src || src.type !== 'category' || src.id === cat) return;
  _reorderTestsByCategory(src.id, cat, true);
}

function dragStartTest(ev, id) {
  _dragState = {type: 'test', id};
  ev.dataTransfer.effectAllowed = 'move';
  ev.dataTransfer.setData('text/plain', `test:${id}`);
}

function dragOverTest(ev) {
  ev.preventDefault();
  ev.dataTransfer.dropEffect = 'move';
}

function dropTest(ev, id) {
  ev.preventDefault();
  const src = _dragState;
  _dragState = null;
  if (!src || src.type !== 'test' || src.id === id) return;
  _reorderTestWithinCategory(src.id, id, true);
}

function setResponsesView(mode) {
  _responsesViewMode = mode;
  loadResponses();
}

function _initCatDropdowns() {
  ['filterCat'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = _catOptions('', true);
  });
  ['new_category','generate_category'].forEach(id => {
    const el = document.getElementById(id);
    if (el && id === 'new_category') el.innerHTML = _catOptions('');
    else if (el) el.innerHTML = '<option value="">Any category</option>' + _catOptions('', false, false);
  });
  _dedupeSelectOptions('filterCat');
  _dedupeSelectOptions('new_category');
  _dedupeSelectOptions('generate_category');
  _dedupeSelectOptions('filterType');
}

function autoGenerateId() {
  const cat = document.getElementById('new_category').value;
  if (!cat) { alert('Select a category first'); return; }
  const existing = tests.filter(t => t.category === cat);
  const maxNum = Math.max(0, ...existing.map(t => {
    const m = t.id.match(new RegExp(cat + '_(\\d+)$'));
    return m ? parseInt(m[1]) : 0;
  }));
  document.getElementById('new_id').value = `${cat}_${String(maxNum + 1).padStart(2, '0')}`;
}

async function load() {
  _initCatDropdowns();
  try {
    const res = await fetch('/api/tests');
    tests = await res.json();
    if (tests.error) throw new Error(tests.error);
    collapsedCats = new Set(CAT_ORDER);
    selectedTestIds = new Set();
    render();
    showTab('main');
  } catch(e) {
    document.getElementById('testList').innerHTML = '<p style="color:#ef4444;padding:20px">Failed to load test cases. Check GitHub env vars.</p>';
  }
}

function render() {
  const search = document.getElementById('search').value.toLowerCase();
  const catEl = document.getElementById('filterCat');
  const typeEl = document.getElementById('filterType');
  const catF = catEl ? catEl.value : '';
  const typeF = typeEl ? typeEl.value : '';
  _sortBy      = 'default';

  let filtered = tests.filter(t =>
    (!search || t.id.includes(search) || t.name.toLowerCase().includes(search) ||
     (t.message||'').toLowerCase().includes(search) ||
     (t.start_message||'').toLowerCase().includes(search) ||
     (Array.isArray(t.followups) ? t.followups.join(' ') : (t.followups||'')).toLowerCase().includes(search) ||
     (t.pass_criteria||'').toLowerCase().includes(search)) &&
    (!catF  || t.category === catF) &&
    (!typeF || t.type === typeF)
  );

  updateSelectionControls(filtered);

  const bycat = {};
  filtered.forEach(t => {
    if (!bycat[t.category]) bycat[t.category] = [];
    bycat[t.category].push(t);
  });

  const filteredCatOrder = _categoryOrderFromTests(filtered);
  const orderedCats = filteredCatOrder
    .concat(Object.keys(bycat).filter(c => !filteredCatOrder.includes(c)));

  if (!orderedCats.length) {
    document.getElementById('testList').innerHTML = '<p style="color:#94a3b8;padding:20px">No tests match filter.</p>';
    return;
  }

  let rows = '';
  orderedCats.forEach(cat => {
    const items = bycat[cat];
    const m = CAT_META[cat] || {label:cat, color:'#334155', bg:'#f8fafc'};
    const isCollapsed = collapsedCats.has(cat);
    const chevron = isCollapsed ? '▸' : '▾';
    const checked = items.every(t => selectedTestIds.has(t.id)) ? 'checked' : '';
    rows += `<tr class="cat-row" data-cat-header="${cat}"
                 ondragover="dragOverCategory(event)"
                 ondrop="dropCategory(event, '${cat}')">
      <td colspan="6" style="color:${m.color}">
        <button class="action-btn drag-handle" draggable="true" onclick="event.stopPropagation()" ondragstart="dragStartCategory(event, '${cat}')" ondragend="endDrag()"
                title="Drag category" style="padding:2px 6px;margin-right:4px;color:${m.color};cursor:grab;">⋮⋮</button>
        <button class="action-btn" onclick="toggleCat('${cat}')" style="padding:2px 6px;margin-right:4px;color:${m.color}">${chevron}</button>
        <label style="display:inline-flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="catchk_${cat}" data-cat-select="${cat}" onclick="toggleCategorySelection('${cat}', this.checked)" ${checked}>
          <span style="background:${m.bg};padding:2px 10px;border-radius:99px">${m.label}</span>
        </label>
        <span style="color:#94a3b8;font-weight:400;margin-left:6px">${items.length} test${items.length!==1?'s':''}</span>
      </td>
      <td style="text-align:right"></td>
    </tr>`;
    items.forEach(t => { rows += renderRow(t, cat); });
  });

  document.getElementById('testList').innerHTML =
    `<table class="test-table">
      <thead><tr>
        <th style="width:44px"><input type="checkbox" id="selectAllTests" onchange="toggleAllVisibleTests(this.checked)"></th>
        <th style="width:110px">ID</th>
        <th>Name</th>
        <th style="width:100px">Type</th>
        <th>Message preview</th>
        <th style="width:68px"></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;

  _applyCollapsed();
  _updateSelectionAfterRender();

}

function renderRow(t, cat) {
  const idx      = tests.indexOf(t);
  const m        = CAT_META[t.category] || {label:t.category, color:'#334155', bg:'#f8fafc'};
  const msgs     = t.messages ? t.messages.join('\\n') : '';
  const followups = t.followups ? t.followups.join('\\n') : '';
  const previewSrc = t.start_message || t.message || (t.messages||[]).join(' · ') || '';
  const preview  = esc((previewSrc || '').substring(0, 80));
  const preWarn  = t.precondition ? ' ⚠' : '';
  const checked   = selectedTestIds.has(t.id) ? 'checked' : '';

  return `
  <tr class="test-row" id="row_${t.id}" data-cat="${cat}" onclick="editRow('${t.id}')"
      ondragover="dragOverTest(event)" ondrop="dropTest(event, '${t.id}')">
    <td onclick="event.stopPropagation()">
      <button class="action-btn drag-handle" draggable="true" onclick="event.stopPropagation()" ondragstart="dragStartTest(event, '${t.id}')" ondragend="endDrag()"
              title="Drag test" style="padding:2px 4px;margin-right:4px;cursor:grab;">⋮⋮</button>
      <input type="checkbox" id="testchk_${t.id}" onchange="toggleTestSelection('${t.id}', this.checked)" ${checked}>
    </td>
    <td class="id-cell">${esc(t.id)}</td>
    <td style="font-weight:600">${esc(t.name)}${preWarn}</td>
    <td><span class="type-pill">${t.type}</span></td>
    <td class="msg-cell" title="${preview}">${preview}</td>
    <td onclick="event.stopPropagation()" style="white-space:nowrap">
      <button class="action-btn" onclick="editRow('${t.id}')" title="Edit">✎</button>
      <button class="action-btn" onclick="deleteTest(${idx})" title="Delete">🗑</button>
    </td>
  </tr>
  <tr class="edit-row" id="editrow_${t.id}" data-cat="${cat}" style="display:none">
    <td colspan="6">
      <div class="edit-form-inner">
        <div class="form-grid">
          <div><label>ID</label><input type="text" value="${esc(t.id)}" onchange="update(${idx},'id',this.value)"></div>
          <div><label>Name</label><input type="text" value="${esc(t.name)}" onchange="update(${idx},'name',this.value)"></div>
          <div><label>Category</label>
            <select onchange="update(${idx},'category',this.value)">${_catOptions(t.category)}</select>
          </div>
          <div><label>Type</label>
            <select onchange="update(${idx},'type',this.value)">
              ${['single','burst','sequence','dedup','burst_with_setup','interactive']
                .map(tp=>`<option value="${tp}" ${t.type===tp?'selected':''}>${tp}</option>`).join('')}
            </select>
          </div>
          ${t.start_message !== undefined || t.type === 'interactive' ? `
          <div class="form-full"><label>Start Message</label>
            <input type="text" value="${esc(t.start_message || t.message || '')}" onchange="update(${idx},'start_message',this.value||undefined)">
          </div>
          <div class="form-full"><label>Follow-up Replies (one per line)</label>
            <textarea onchange="updateInteractiveFollowups(${idx},this.value)">${esc(followups)}</textarea>
          </div>
          <div><label>Auto Continue</label>
            <select onchange="update(${idx},'auto_continue',this.value==='true')">
              <option value="true" ${(t.auto_continue !== false) ? 'selected' : ''}>On</option>
              <option value="false" ${t.auto_continue === false ? 'selected' : ''}>Off</option>
            </select>
          </div>
          <div><label>Max Turns</label>
            <input type="text" value="${t.max_turns || ''}" onchange="update(${idx},'max_turns',parseInt(this.value)||undefined)">
          </div>
          <div class="form-full"><label>Stop When</label>
            <input type="text" value="${esc(Array.isArray(t.stop_when) ? t.stop_when.join('\\n') : (t.stop_when || ''))}" onchange="update(${idx},'stop_when',this.value||undefined)">
          </div>` : ''}
          ${t.message !== undefined && t.type !== 'interactive' ? `
          <div class="form-full"><label>Message</label>
            <input type="text" value="${esc(t.message||'')}" onchange="update(${idx},'message',this.value)">
          </div>` : ''}
          ${t.messages !== undefined ? `
          <div class="form-full"><label>Messages (one per line)</label>
            <textarea onchange="updateMsgs(${idx},this.value)">${esc(msgs)}</textarea>
          </div>` : ''}
          <div><label>Wait (s)</label>
            <input type="text" value="${t.wait||120}" onchange="update(${idx},'wait',parseInt(this.value))">
          </div>
          <div><label>Expected Responses</label>
            <input type="text" value="${t.expected_responses||''}" onchange="update(${idx},'expected_responses',parseInt(this.value)||undefined)">
          </div>
          <div class="form-full"><label>Pass Criteria</label>
            <textarea onchange="update(${idx},'pass_criteria',this.value)">${esc(t.pass_criteria||'')}</textarea>
          </div>
          <div class="form-full"><label>Precondition</label>
            <input type="text" value="${esc(t.precondition||'')}" onchange="update(${idx},'precondition',this.value||undefined)">
          </div>
          <div class="form-full"><label>Notes</label>
            <input type="text" value="${esc(t.note||t.manual_check||'')}" onchange="update(${idx},'note',this.value||undefined)">
          </div>
        </div>
        <div class="form-actions">
          <button class="btn btn-run" onclick="runById('${t.id}')">▶ Run this test</button>
          <button class="btn btn-success" onclick="saveAll()">💾 Save</button>
          <button class="btn btn-danger" onclick="deleteTest(${idx})">Delete</button>
          <button class="btn btn-outline" onclick="editRow('${t.id}')">✕ Close</button>
        </div>
        <div class="inline-result" id="result_${t.id}"></div>
      </div>
    </td>
  </tr>`;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}

function editRow(id) {
  const editEl = document.getElementById('editrow_' + id);
  const rowEl  = document.getElementById('row_' + id);
  if (!editEl) return;
  const isOpen = editEl.style.display !== 'none';
  // Close previously open row
  if (_editingId && _editingId !== id) {
    const prev    = document.getElementById('editrow_' + _editingId);
    const prevRow = document.getElementById('row_'     + _editingId);
    if (prev) prev.style.display = 'none';
    if (prevRow) prevRow.classList.remove('editing');
  }
  editEl.style.display = isOpen ? 'none' : '';
  rowEl && rowEl.classList.toggle('editing', !isOpen);
  _editingId = isOpen ? null : id;
}

function filter() { render(); }

function toggleCat(cat) {
  if (collapsedCats.has(cat)) {
    collapsedCats.delete(cat);
  } else {
    collapsedCats.add(cat);
    _closeEditRowsForCategory(cat);
  }
  _applyCollapsed();
}

function collapseAll() {
  document.querySelectorAll('tr[data-cat]').forEach(row => {
    const cat = row.getAttribute('data-cat');
    collapsedCats.add(cat);
    _closeEditRowsForCategory(cat);
  });
  _applyCollapsed();
}

function expandAll() {
  collapsedCats.clear();
  _applyCollapsed();
}

function _closeEditRowsForCategory(cat) {
  document.querySelectorAll(`tr.edit-row[data-cat="${cat}"]`).forEach(el => {
    el.style.display = 'none';
  });
  document.querySelectorAll(`tr.test-row[data-cat="${cat}"]`).forEach(el => {
    el.classList.remove('editing');
  });
  if (_editingId) {
    const editEl = document.getElementById('editrow_' + _editingId);
    if (editEl && editEl.getAttribute('data-cat') === cat) {
      _editingId = null;
    }
  }
}

function _applyCollapsed() {
  document.querySelectorAll('tr[data-cat]').forEach(row => {
    const cat = row.getAttribute('data-cat');
    if (row.classList.contains('edit-row')) {
      row.style.display = 'none';
      return;
    }
    if (collapsedCats.has(cat)) {
      row.style.display = 'none';
    } else {
      row.style.display = '';
      row.classList.remove('editing');
    }
  });
  // Update chevron icons
  document.querySelectorAll('.cat-row[data-cat-header]').forEach(row => {
    const btn = row.querySelector('.action-btn');
    if (btn) {
      const cat = row.getAttribute('data-cat-header');
      btn.textContent = collapsedCats.has(cat) ? '▸' : '▾';
    }
  });
}

function update(idx, key, val) {
  if (val === undefined || val === '') delete tests[idx][key];
  else tests[idx][key] = val;
}

function updateMsgs(idx, val) {
  tests[idx].messages = val.split('\\n').map(s=>s.trim()).filter(Boolean);
}

function updateInteractiveFollowups(idx, val) {
  tests[idx].followups = val.split('\\n').map(s=>s.trim()).filter(Boolean);
}

function toggleNew() {
  const form = document.getElementById('newForm');
  const isOpening = !form.classList.contains('open');
  form.classList.toggle('open');
  if (isOpening) {
    document.getElementById('new_name').value = '';
    document.getElementById('new_message').value = '';
    document.getElementById('new_messages').value = '';
    document.getElementById('new_followups').value = '';
    document.getElementById('new_pass_criteria').value = '';
    document.getElementById('new_precondition').value = '';
    document.getElementById('new_manual_check').value = '';
    document.getElementById('new_type').value = 'single';
    document.getElementById('new_auto_continue').value = 'true';
    document.getElementById('new_max_turns').value = '';
    document.getElementById('new_stop_when').value = '';
    _initCatDropdowns();
    toggleNewMsgFields();
  }
}

function toggleNewMsgFields() {
  const type = document.getElementById('new_type').value;
  const multi = ['burst','sequence','burst_with_setup'].includes(type);
  const interactive = type === 'interactive';
  const msgInput = document.getElementById('new_message');
  document.getElementById('new_msg_wrap').style.display  = multi ? 'none' : '';
  document.getElementById('new_msgs_wrap').style.display = multi ? '' : 'none';
  document.getElementById('new_interactive_wrap').style.display = interactive ? '' : 'none';
  document.getElementById('new_wait_wrap').style.display = (multi || interactive) ? '' : 'none';
  if (msgInput) msgInput.placeholder = interactive ? 'Start message' : 'Exact iMessage to send to Asmi';
}

function addNew() {
  const type = document.getElementById('new_type').value;
  const category = document.getElementById('new_category').value.trim();
  const tc = {
    name:         document.getElementById('new_name').value.trim(),
    category:     category,
    type:         type,
    pass_criteria: document.getElementById('new_pass_criteria').value.trim(),
  };
  if (['burst','sequence','burst_with_setup'].includes(type)) {
    tc.messages = document.getElementById('new_messages').value.split('\\n').map(s=>s.trim()).filter(Boolean);
    const waitVal = document.getElementById('new_wait_preset').value;
    if (waitVal) tc.wait = parseInt(waitVal);
  } else if (type === 'interactive') {
    tc.start_message = document.getElementById('new_message').value.trim();
    tc.followups = document.getElementById('new_followups').value.split('\\n').map(s=>s.trim()).filter(Boolean);
    const ac = document.getElementById('new_auto_continue').value;
    tc.auto_continue = ac !== 'false';
    const mt = parseInt(document.getElementById('new_max_turns').value);
    if (mt) tc.max_turns = mt;
    const sw = document.getElementById('new_stop_when').value.trim();
    if (sw) tc.stop_when = sw.split(',').map(s => s.trim()).filter(Boolean);
  } else {
    tc.message = document.getElementById('new_message').value.trim();
  }
  const pre  = document.getElementById('new_precondition').value.trim();
  const mchk = document.getElementById('new_manual_check').value.trim();
  if (pre)  tc.precondition  = pre;
  if (mchk) tc.manual_check  = mchk;

  if (!tc.name) { alert('Name is required'); return; }

  const existingIds = tests.filter(t => t.category === category).map(t => t.id);
  let seq = 1;
  while (existingIds.includes(category.substring(0,3).toLowerCase() + '_' + String(seq).padStart(2,'0'))) seq++;
  tc.id = category.substring(0,3).toLowerCase() + '_' + String(seq).padStart(2,'0');

  tests.push(tc);
  toggleNew();
  render();
}

function deleteTest(idx) {
  if (!confirm('Delete this test case?')) return;
  tests.splice(idx, 1);
  render();
}

async function saveAll() {
  const btn = document.getElementById('saveBtn');
  if (btn) {
    btn.textContent = 'Saving…';
    btn.classList.add('saving');
  }
  try {
    const res = await fetch('/api/tests', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(tests),
    });
    const data = await res.json();
    if (data.ok) toast('Saved');
    else alert('Save failed: ' + data.error);
  } catch(e) {
    alert('Save failed: ' + e.message);
  } finally {
    if (btn) {
      btn.textContent = 'Save';
      btn.classList.remove('saving');
    }
  }
}

let _pollTimer      = null;
let _runStart       = 0;
let _activeTestId   = null;
let _lastRunAnalysis = '';
let _historyRefreshTimer = null;
let selectedTestIds = new Set();
let interactiveAutoContinue = true;

async function runSelected() {
  const ids = tests.filter(t => selectedTestIds.has(t.id)).map(t => t.id);
  if (!ids.length) {
    toast('Select at least one test to run');
    return;
  }
  if (ids.length === 1) {
    await _triggerRun({id: ids[0], interactive_auto_continue: interactiveAutoContinue});
  } else {
    await _triggerRun({ids, interactive_auto_continue: interactiveAutoContinue});
  }
}

function toggleInteractiveAutoContinue() {
  interactiveAutoContinue = !interactiveAutoContinue;
  const btn = document.getElementById('interactiveAutoBtn');
  if (btn) btn.textContent = interactiveAutoContinue ? 'Auto-continue: On' : 'Auto-continue: Off';
  toast(interactiveAutoContinue ? 'Interactive auto-continue enabled' : 'Interactive auto-continue paused');
}

function updateSelectionControls(visibleTests = tests) {
  const count = selectedTestIds.size;
  const label = count === 1 ? '1 test selected' : `${count} tests selected`;
  const el = document.getElementById('selectedBadge');
  if (el) el.textContent = label;
  const visibleIds = visibleTests.map(t => t.id);
  const selectedVisible = visibleIds.filter(id => selectedTestIds.has(id)).length;
  const all = document.getElementById('selectAllTests');
  if (all) {
    all.checked = visibleIds.length > 0 && selectedVisible === visibleIds.length;
    all.indeterminate = selectedVisible > 0 && selectedVisible < visibleIds.length;
  }
  document.querySelectorAll('input[data-cat-select]').forEach(el => {
    const cat = el.getAttribute('data-cat-select');
    const items = visibleTests.filter(t => t.category === cat);
    const selected = items.filter(t => selectedTestIds.has(t.id)).length;
    el.checked = items.length > 0 && selected === items.length;
    el.indeterminate = selected > 0 && selected < items.length;
    if (!items.length) {
      el.checked = false;
      el.indeterminate = false;
    }
  });
}

function _visibleTests() {
  const search = document.getElementById('search').value.toLowerCase();
  const catEl = document.getElementById('filterCat');
  const typeEl = document.getElementById('filterType');
  const catF = catEl ? catEl.value : '';
  const typeF = typeEl ? typeEl.value : '';
  return tests.filter(t =>
    (!search || t.id.includes(search) || t.name.toLowerCase().includes(search) ||
     (t.message||'').toLowerCase().includes(search) ||
     (t.start_message||'').toLowerCase().includes(search) ||
     (Array.isArray(t.followups) ? t.followups.join(' ') : (t.followups||'')).toLowerCase().includes(search) ||
     (t.pass_criteria||'').toLowerCase().includes(search)) &&
    (!catF  || t.category === catF) &&
    (!typeF || t.type === typeF)
  );
}

function _updateSelectionAfterRender() {
  const visibleTests = _visibleTests();
  updateSelectionControls(visibleTests);
  visibleTests.forEach(t => {
    const el = document.getElementById('testchk_' + t.id);
    if (el) el.checked = selectedTestIds.has(t.id);
  });
  const bycat = {};
  visibleTests.forEach(t => {
    if (!bycat[t.category]) bycat[t.category] = [];
    bycat[t.category].push(t);
  });
  Object.keys(bycat).forEach(cat => {
    const el = document.getElementById('catchk_' + cat);
    if (!el) return;
    const items = bycat[cat];
    const selected = items.filter(t => selectedTestIds.has(t.id)).length;
    el.checked = selected === items.length;
    el.indeterminate = selected > 0 && selected < items.length;
  });
}

function updateSelectedBadge() {
  updateSelectionControls();
}

function toggleCategorySelection(cat, checked) {
  tests.filter(t => t.category === cat).forEach(t => {
    if (checked) selectedTestIds.add(t.id);
    else selectedTestIds.delete(t.id);
  });
  render();
}

function toggleTestSelection(id, checked) {
  if (checked) selectedTestIds.add(id);
  else selectedTestIds.delete(id);
  render();
}

function toggleAllVisibleTests(checked) {
  _visibleTests().forEach(t => {
    const id = t.id;
    if (checked) selectedTestIds.add(id);
    else selectedTestIds.delete(id);
  });
  render();
}

function selectAllCategories() {
  const allSelected = selectedTestIds.size === tests.length;
  if (allSelected) selectedTestIds.clear();
  else tests.forEach(t => selectedTestIds.add(t.id));
  render();
}

async function runById(id) {
  _activeTestId = id;
  // Keep the row open if the user already expanded it; otherwise open it.
  const editEl = document.getElementById('editrow_' + id);
  if (editEl && editEl.style.display === 'none') editRow(id);
  // Show running indicator inline
  const inlineEl = document.getElementById('result_' + id);
  if (inlineEl) {
    inlineEl.className = 'inline-result running';
    inlineEl.innerHTML = '<span class="inline-running-dots">Running</span>';
  }
  const btn = document.getElementById('runbtn_' + id);
  if (btn) btn.disabled = true;
  await _triggerRun({id, interactive_auto_continue: interactiveAutoContinue});
}

async function runByCategory(cat) {
  await _triggerRun({category: cat, interactive_auto_continue: interactiveAutoContinue});
}

async function _triggerRun(payload) {
  payload = {...payload, interactive_auto_continue: payload.interactive_auto_continue ?? interactiveAutoContinue};
  const label = payload.id ? `test: ${payload.id}` :
                payload.ids ? `${payload.ids.length} selected tests` :
                payload.category ? `category: ${payload.category}` :
                payload.categories ? `categories: ${payload.categories.join(',')}` : 'all tests';
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (!data.ok) {
      toast(data.error || 'Run request failed');
      return;
    }
    toast(`Queued ${label}…`);
    _openOutput(`Queued ${label}…`);
  } catch(e) {
    toast('Failed to queue run: ' + e.message);
  }
}

function _openOutput(label) {
  _runStart = Date.now();
  const panel = document.getElementById('outputPanel');
  document.getElementById('resultsTable').style.display = 'none';
  document.getElementById('resultsTable').innerHTML = '';
  document.getElementById('outputBodyText').textContent = '';
  document.getElementById('outputElapsed').textContent = '';
  document.getElementById('behaviorAnalysisPanel').style.display = 'none';
  document.getElementById('behaviorAnalysisBox').textContent = '';
  _lastRunAnalysis = '';
  document.getElementById('stopBtn').style.display = 'inline-block';
  // For single-test runs, hide the top panel — result shows inline in card
  if (_activeTestId) {
    panel.style.display = 'none';
  } else {
    panel.style.display = 'block';
    document.getElementById('outputStatus').textContent = label || 'Waiting for daemon…';
    panel.scrollIntoView({behavior:'smooth', block:'start'});
  }
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(_pollOutput, 3000);
}

async function stopRun() {
  document.getElementById('stopBtn').style.display = 'none';
  document.getElementById('outputStatus').textContent = 'Stopping…';
  try { await fetch('/api/stop', {method:'POST'}); } catch(e) {}
  toast('Stop signal sent — daemon will halt after current test');
}

function _cleanOutput(text) {
  // Strip the "Report: ..." trailing line from daemon output text
  return (text || '')
    .split('\\n')
    .filter(l => !l.startsWith('Report:') && !l.startsWith('  Report:'))
    .join('\\n')
    .trim();
}

async function _pollOutput() {
  try {
    const res  = await fetch('/api/output');
    const data = await res.json();
    const secs = Math.round((Date.now() - _runStart) / 1000);
    if (!_activeTestId)
      document.getElementById('outputElapsed').textContent = `${secs}s elapsed`;

    if (data.status === 'running') {
      if (!_activeTestId)
        document.getElementById('outputStatus').textContent = 'Running…';
      // Update inline elapsed for active single test
      if (_activeTestId) {
        const inlineEl = document.getElementById('result_' + _activeTestId);
        if (inlineEl && inlineEl.classList.contains('running'))
          inlineEl.innerHTML = `<span class="inline-running-dots">Running</span> <span style="color:#94a3b8;font-size:0.78rem">${secs}s</span>`;
      }
      // Update progress bar
      if (!_activeTestId && data.progress && data.progress.total > 0) {
        const p = data.progress;
        const pct = Math.round(p.completed / p.total * 100);
        document.getElementById('progressBar').style.display = 'block';
        document.getElementById('progressLabel').textContent =
          p.current_test ? `[${p.current_test}]${p.current_category ? '  ' + p.current_category : ''}` : 'Starting…';
        document.getElementById('progressPct').textContent = `${p.completed} / ${p.total}  (${pct}%)`;
        document.getElementById('progressFill').style.width = pct + '%';
      }
    } else if (data.status === 'done' || data.status === 'stopped') {
      clearInterval(_pollTimer);
      document.getElementById('stopBtn').style.display = 'none';
      document.getElementById('progressBar').style.display = 'none';
      const secs2 = Math.round((Date.now() - _runStart) / 1000);
      if (data.status === 'stopped') {
        document.getElementById('outputStatus').textContent = 'Stopped';
        document.getElementById('outputElapsed').textContent = `${secs2}s`;
      }

      if (data.results && data.results.length > 0) {
        // Multi-test: keep completion compact and direct user to Reports tab
        if (!_activeTestId) {
          document.getElementById('outputPanel').style.display = 'block';
          document.getElementById('outputStatus').textContent = 'Done';
          document.getElementById('outputElapsed').textContent = `${secs2}s elapsed`;
          document.getElementById('resultsTable').style.display = 'none';
          document.getElementById('resultsTable').innerHTML = '';
          document.getElementById('behaviorAnalysisPanel').style.display = 'none';
          document.getElementById('outputBodyText').textContent = 'Run complete. Open the Reports tab to view results and download the report.';
          toast('Run complete. Check Reports tab.');
        }
        // Single-test: render inline in card
        if (_activeTestId) {
          const r = data.results.find(x => x.id === _activeTestId);
          if (r) _renderInlineResult(_activeTestId, r);
          const btn = document.getElementById('runbtn_' + _activeTestId);
          if (btn) btn.disabled = false;
          _activeTestId = null;
        }
      } else {
        // No structured results — show cleaned text
        const cleaned = _cleanOutput(data.output);
        if (_activeTestId) {
          const inlineEl = document.getElementById('result_' + _activeTestId);
          if (inlineEl) {
            inlineEl.className = 'inline-result done';
            inlineEl.innerHTML = `<div class="rt-card" style="border-left:4px solid #94a3b8">
              <div class="rt-card-hdr"><span class="rt-tname" style="color:#64748b">Run complete</span>
              <span class="rt-dur">${secs2}s</span></div>
              <div class="rt-section"><div class="rt-slabel">Output</div>
              <div class="rt-judge" style="font-family:monospace;white-space:pre-wrap">${esc(cleaned)}</div></div></div>`;
          }
          const btn = document.getElementById('runbtn_' + _activeTestId);
          if (btn) btn.disabled = false;
          _activeTestId = null;
        } else {
          document.getElementById('outputPanel').style.display = 'block';
          document.getElementById('outputStatus').textContent = 'Done';
          document.getElementById('outputElapsed').textContent = `${secs2}s elapsed`;
          document.getElementById('outputBodyText').textContent = cleaned;
        }
      }
      _refreshHistoryTabs();
    }
  } catch(e) {}
}

function _refreshHistoryTabs() {
  const historySection = document.getElementById('historySection');
  const responsesSection = document.getElementById('responsesSection');
  const analysisSection = document.getElementById('analysisSection');
  if (historySection && historySection.style.display !== 'none') loadHistory();
  if (responsesSection && responsesSection.style.display !== 'none') loadResponses();
  if (analysisSection && analysisSection.style.display !== 'none') loadAnalysis();
}

function _startHistoryAutoRefresh() {
  if (_historyRefreshTimer) clearInterval(_historyRefreshTimer);
  _historyRefreshTimer = setInterval(_refreshHistoryTabs, 5000);
}

function _resultCard(r) {
  const v    = (r.verdict || 'UNCLEAR').toUpperCase();
  const vcls = v === 'PASS' ? 'rt-pass' : v === 'FAIL' ? 'rt-fail' : 'rt-unclear';

  const started  = r.started_at  ? new Date(r.started_at)  : null;
  const finished = r.finished_at ? new Date(r.finished_at) : null;
  const dur = started && finished ? Math.round((finished - started) / 1000) + 's' : '';

  const tasks = (r.tasks_sent || []).map(t =>
    `<div class="rt-msg">${esc(t)}</div>`).join('');
  const resps = (r.responses || []).map((rsp, i) =>
    `<div class="rt-msg"><span class="rt-resp-num">Response ${i+1}</span>${esc(rsp)}</div>`).join('');
  const transcript = (r.transcript || []).map(turn => {
    const turnResps = (turn.responses || []).map((rsp, i) =>
      `<div class="rt-msg"><span class="rt-resp-num">Asmi ${i+1}</span>${esc(rsp)}</div>`).join('');
    return `<div class="rt-turn">
      <div class="rt-turn-head">Turn ${turn.turn || ''}</div>
      <div class="rt-msg rt-user"><span class="rt-resp-num">You</span>${esc(turn.user || '')}</div>
      ${turnResps}
    </div>`;
  }).join('');

  return `<div class="rt-card ${vcls}">
    <div class="rt-card-hdr">
      <span class="rt-tid">[${esc(r.id||'')}]</span>
      <span class="rt-tname">${esc(r.name||'')}</span>
      <span class="rt-badge ${vcls}">${v}</span>
      ${dur ? `<span class="rt-dur">${dur}</span>` : ''}
    </div>
    ${tasks ? `<div class="rt-section"><div class="rt-slabel">Task sent</div>${tasks}</div>` : ''}
    ${resps ? `<div class="rt-section"><div class="rt-slabel">Responses (${(r.responses||[]).length})</div>${resps}</div>` : ''}
    ${transcript ? `<div class="rt-section"><div class="rt-slabel">Transcript</div>${transcript}</div>` : ''}
    ${r.reason ? `<div class="rt-section"><div class="rt-slabel">Judge</div><div class="rt-judge">${esc(r.reason)}</div></div>` : ''}
  </div>`;
}

function _renderInlineResult(id, r) {
  const inlineEl = document.getElementById('result_' + id);
  if (!inlineEl) return;
  inlineEl.className = 'inline-result done';
  inlineEl.innerHTML = _resultCard(r);
}

function _renderResults(results) {
  const total   = results.length;
  const passed  = results.filter(r => r.verdict === 'PASS').length;
  const failed  = results.filter(r => r.verdict === 'FAIL').length;
  const unclear = total - passed - failed;
  const pct     = total ? Math.round(passed / total * 100) : 0;
  const pctColor = pct === 100 ? '#16a34a' : pct >= 70 ? '#b45309' : '#dc2626';

  const cards = results.map(_resultCard).join('');

  const el = document.getElementById('resultsTable');
  el.style.display = 'block';
  el.innerHTML = `
    <div class="rt-summary">
      <span class="rt-pct" style="color:${pctColor}">${pct}%</span>
      <span class="rt-pass">${passed} passed</span>
      <span class="rt-fail">${failed} failed</span>
      ${unclear ? `<span class="rt-unclear">${unclear} unclear</span>` : ''}
      <span style="color:#94a3b8;font-weight:400">${total} total</span>
    </div>
    ${cards}`;
}

function clearOutput() {
  document.getElementById('outputPanel').style.display = 'none';
  document.getElementById('resultsTable').style.display = 'none';
  document.getElementById('behaviorAnalysisPanel').style.display = 'none';
  document.getElementById('stopBtn').style.display = 'none';
  document.getElementById('progressBar').style.display = 'none';
  if (_pollTimer) clearInterval(_pollTimer);
}

async function _runBehaviorAnalysis(results) {
  const panel = document.getElementById('behaviorAnalysisPanel');
  const box   = document.getElementById('behaviorAnalysisBox');
  panel.style.display = 'block';
  box.textContent = 'Analyzing all responses together…';
  try {
    const res  = await fetch('/api/analyze', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({results}),
    });
    const data = await res.json();
    if (data.ok) {
      _lastRunAnalysis = data.analysis;
      box.textContent  = data.analysis;
      // Auto-save to ASMI_BEHAVIOR_ANALYSIS.md
      await fetch('/api/save-behavior', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({content: data.analysis}),
      });
      toast('Behavior analysis saved to ASMI_BEHAVIOR_ANALYSIS.md');
    } else {
      box.textContent = 'Analysis failed: ' + data.error;
    }
  } catch(e) {
    box.textContent = 'Analysis error: ' + e.message;
  }
}

async function saveBehaviorFromRun() {
  if (!_lastRunAnalysis) { alert('No analysis yet'); return; }
  try {
    const res  = await fetch('/api/save-behavior', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({content: _lastRunAnalysis}),
    });
    const data = await res.json();
    if (data.ok) toast('Saved to ASMI_BEHAVIOR_ANALYSIS.md');
    else alert('Save failed: ' + data.error);
  } catch(e) { alert('Save error: ' + e.message); }
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 3000);
}

// ── Test Case Generation Functions ───────────────────────────────────────────

let generatedTests = [];
let _genPollTimer  = null;
let _genRunStart   = 0;
let _lastAnalysis  = '';

function toggleGenerate() {
  document.getElementById('generateForm').classList.toggle('open');
}

async function generateTests() {
  const prompt = document.getElementById('generate_prompt').value.trim();
  const count  = parseInt(document.getElementById('generate_count').value);
  const cat    = document.getElementById('generate_category').value;
  if (!prompt) { alert('Please enter a test scenario description'); return; }

  const btn = document.getElementById('generateBtn');
  const btnText = document.getElementById('generateBtnText');
  btnText.textContent = 'Generating…';
  btn.disabled = true;

  try {
    const res  = await fetch('/api/generate', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({prompt, count, category: cat}),
    });
    const data = await res.json();
    if (data.ok && data.cases) {
      generatedTests = data.cases.map(t => ({...t, category: 'generated'}));
      renderGenerated();
      document.getElementById('generatedPreview').style.display = 'block';
      document.getElementById('genAnalysisSection').style.display = 'none';
      document.getElementById('genRunStatus').textContent = '';
      toast(`Generated ${data.cases.length} test cases`);
    } else {
      alert('Generation failed: ' + (data.error || 'Unknown error'));
    }
  } catch(e) {
    alert('Generation failed: ' + e.message);
  } finally {
    btnText.textContent = '🤖 Generate Tests';
    btn.disabled = false;
  }
}

function renderGenerated() {
  const el = document.getElementById('generatedList');
  el.innerHTML = generatedTests.map((test, idx) => `
    <div id="gen_card_${idx}" style="margin-bottom:12px;padding:12px;border:1px solid #e2e8f0;border-radius:8px;background:white;">
      <div style="display:flex;align-items:flex-start;gap:8px;margin-bottom:6px;">
        <div style="font-weight:600;color:#374151;flex:1;">${esc(test.name)}</div>
        <button class="btn" style="background:#7c3aed;color:white;padding:4px 10px;font-size:0.78rem;flex-shrink:0;"
          id="genrunbtn_${idx}" onclick="runSingleGenerated(${idx})">▶ Run</button>
      </div>
      <div style="font-size:0.82rem;color:#64748b;margin-bottom:6px;">
        <span class="badge badge-cat">generated</span>
        <span class="badge badge-type">${esc(test.type)}</span>
        <span style="color:#94a3b8;margin-left:4px;">${test.expected_responses||1} response(s) expected</span>
      </div>
      <div style="font-size:0.85rem;color:#374151;margin-bottom:4px;">
        <strong>Message:</strong> ${esc(test.message || (test.messages||[]).join(' → '))}
      </div>
      <div style="font-size:0.82rem;color:#64748b;margin-bottom:6px;">
        <strong>Criteria:</strong> ${esc(test.pass_criteria)}
      </div>
      <div id="gen_result_${idx}" style="display:none;margin-top:8px;"></div>
    </div>
  `).join('');
}

async function saveAndRunGenerated() {
  if (generatedTests.length === 0) return;
  const btn = document.getElementById('runGenBtn');
  btn.disabled = true;
  document.getElementById('genRunStatus').textContent = 'Saving tests…';

  // Merge generated tests into main test list and save
  const merged = tests.filter(t => t.category !== 'generated').concat(generatedTests);
  try {
    await fetch('/api/tests', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(merged)});
  } catch(e) { alert('Save failed: ' + e.message); btn.disabled = false; return; }
  tests = merged;
  render();

  // Trigger run for category "generated"
  document.getElementById('genRunStatus').textContent = 'Queued for daemon…';
  const res  = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({category:'generated'})});
  const data = await res.json();
  if (!data.ok) { alert('Run failed'); btn.disabled = false; return; }
  document.getElementById('genRunStatus').textContent = 'Queued for daemon…';

  _genRunStart = Date.now();
  if (_genPollTimer) clearInterval(_genPollTimer);
  _genPollTimer = setInterval(_pollGeneratedRun, 3000);
}

async function runSingleGenerated(idx) {
  const test = generatedTests[idx];
  const btn  = document.getElementById('genrunbtn_' + idx);
  if (btn) btn.disabled = true;

  // Upsert this single test into the main list and save
  const without = tests.filter(t => t.id !== test.id);
  const merged  = without.concat([test]);
  try {
    await fetch('/api/tests', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(merged)});
    tests = merged;
  } catch(e) { alert('Save failed'); if (btn) btn.disabled = false; return; }

  const resultEl = document.getElementById('gen_result_' + idx);
  resultEl.style.display = 'block';
  resultEl.innerHTML = '<span style="color:#7c3aed;font-size:0.82rem;">Sending to daemon…</span>';

  const res  = await fetch('/api/run', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: test.id})});
  const data = await res.json();
  if (!data.ok) { resultEl.innerHTML = '<span style="color:red">Run failed</span>'; if (btn) btn.disabled = false; return; }

  _genRunStart = Date.now();
  if (_genPollTimer) clearInterval(_genPollTimer);
  _genPollTimer = setInterval(() => _pollSingleGenRun(idx), 3000);
}

async function _pollGeneratedRun() {
  try {
    const res  = await fetch('/api/output');
    const data = await res.json();
    const secs = Math.round((Date.now() - _genRunStart) / 1000);
    if (data.status === 'running') {
      document.getElementById('genRunStatus').textContent = `Running… ${secs}s`;
    } else if (data.status === 'done') {
      clearInterval(_genPollTimer); _genPollTimer = null;
      document.getElementById('genRunStatus').textContent = `Done in ${secs}s`;
      document.getElementById('runGenBtn').disabled = false;
      const results = data.results || [];
      results.forEach(r => {
        const idx = generatedTests.findIndex(t => t.id === r.id);
        if (idx >= 0) _renderGenResult(idx, r);
      });
      if (results.length > 0) _fetchGenAnalysis(results);
    }
  } catch(e) {}
}

async function _pollSingleGenRun(idx) {
  try {
    const res  = await fetch('/api/output');
    const data = await res.json();
    const secs = Math.round((Date.now() - _genRunStart) / 1000);
    const resultEl = document.getElementById('gen_result_' + idx);
    if (data.status === 'running') {
      if (resultEl) resultEl.innerHTML = `<span style="color:#7c3aed;font-size:0.82rem;">Running… ${secs}s</span>`;
    } else if (data.status === 'done') {
      clearInterval(_genPollTimer); _genPollTimer = null;
      const btn = document.getElementById('genrunbtn_' + idx);
      if (btn) btn.disabled = false;
      const test = generatedTests[idx];
      const r = (data.results || []).find(x => x.id === test.id);
      if (r) _renderGenResult(idx, r);
      else if (resultEl) resultEl.innerHTML = '<span style="color:#94a3b8;font-size:0.82rem;">No result returned</span>';
      if (data.results && data.results.length > 0) _fetchGenAnalysis(data.results);
    }
  } catch(e) {}
}

function _renderGenResult(idx, r) {
  const el = document.getElementById('gen_result_' + idx);
  if (!el) return;
  el.style.display = 'block';
  const v    = (r.verdict||'UNCLEAR').toUpperCase();
  const col  = v === 'PASS' ? '#16a34a' : v === 'FAIL' ? '#dc2626' : '#b45309';
  const bg   = v === 'PASS' ? '#f0fdf4' : v === 'FAIL' ? '#fef2f2' : '#fffbeb';
  const resps = (r.responses||[]).map((rsp,i) =>
    `<div style="margin-top:4px;font-size:0.8rem;color:#374151;"><strong>Response ${i+1}:</strong> ${esc(rsp)}</div>`).join('');
  el.innerHTML = `<div style="border-left:3px solid ${col};padding:8px 10px;background:${bg};border-radius:4px;">
    <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px;">
      <span style="font-weight:700;color:${col};font-size:0.85rem;">${v}</span>
      <span style="color:#64748b;font-size:0.8rem;">${esc(r.reason||'')}</span>
    </div>
    ${resps}
  </div>`;
}

async function _fetchGenAnalysis(results) {
  document.getElementById('genAnalysisSection').style.display = 'block';
  document.getElementById('genAnalysisBox').textContent = 'Analyzing all results together…';
  try {
    const res  = await fetch('/api/analyze', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({results}),
    });
    const data = await res.json();
    if (data.ok) {
      _lastAnalysis = data.analysis;
      document.getElementById('genAnalysisBox').textContent = data.analysis;
    } else {
      document.getElementById('genAnalysisBox').textContent = 'Analysis failed: ' + data.error;
    }
  } catch(e) {
    document.getElementById('genAnalysisBox').textContent = 'Analysis error: ' + e.message;
  }
}

async function saveBehaviorAnalysis() {
  if (!_lastAnalysis) { alert('No analysis to save'); return; }
  try {
    const res  = await fetch('/api/save-behavior', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({content: _lastAnalysis}),
    });
    const data = await res.json();
    if (data.ok) toast('Saved to ASMI_BEHAVIOR_ANALYSIS.md');
    else alert('Save failed: ' + data.error);
  } catch(e) { alert('Save error: ' + e.message); }
}

function clearGenerated() {
  generatedTests = [];
  if (_genPollTimer) { clearInterval(_genPollTimer); _genPollTimer = null; }
  document.getElementById('generatedPreview').style.display = 'none';
  document.getElementById('generate_prompt').value = '';
  document.getElementById('genAnalysisSection').style.display = 'none';
}

function _setTabMenu(tab) {
  const map = {
    main: 'menuMain',
    history: 'menuHistory',
    responses: 'menuResponses',
    analysis: 'menuAnalysis',
  };
  Object.values(map).forEach(id => {
    const el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  const activeMenu = document.getElementById(map[tab] || map.main);
  if (activeMenu) activeMenu.style.display = 'flex';
}

function _setActiveTabStyle(tab) {
  const tabMap = {
    main: 'tabMain',
    history: 'tabHistory',
    responses: 'tabResponses',
    analysis: 'tabAnalysis',
  };
  Object.values(tabMap).forEach(id => {
    const el = document.getElementById(id);
    if (!el) return;
    el.classList.remove('active');
  });
  const active = document.getElementById(tabMap[tab] || tabMap.main);
  if (active) active.classList.add('active');
}

function showTab(tab) {
  const mainSection = document.getElementById('mainSection');
  const historySection = document.getElementById('historySection');
  const responsesSection = document.getElementById('responsesSection');
  const analysisSection = document.getElementById('analysisSection');
  const tabMain = document.getElementById('tabMain');
  const tabHistory = document.getElementById('tabHistory');
  const tabResponses = document.getElementById('tabResponses');
  const tabAnalysis = document.getElementById('tabAnalysis');
  _setTabMenu(tab);
  _setActiveTabStyle(tab);

  if (tab === 'history') {
    mainSection.style.display = 'none';
    historySection.style.display = 'block';
    responsesSection.style.display = 'none';
    analysisSection.style.display = 'none';
    loadHistory();
  } else if (tab === 'responses') {
    mainSection.style.display = 'none';
    historySection.style.display = 'none';
    responsesSection.style.display = 'block';
    analysisSection.style.display = 'none';
    loadResponses();
  } else if (tab === 'analysis') {
    mainSection.style.display = 'none';
    historySection.style.display = 'none';
    responsesSection.style.display = 'none';
    analysisSection.style.display = 'block';
    loadAnalysis();
  } else {
    mainSection.style.display = 'block';
    historySection.style.display = 'none';
    responsesSection.style.display = 'none';
    analysisSection.style.display = 'none';
  }
}

async function loadHistory() {
  const historyList = document.getElementById('historyList');
  historyList.innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">Loading…</p>';
  try {
    const res = await fetch('/api/history');
    const data = await res.json();
    if (data.length === 0) {
      historyList.innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">No reports yet.</p>';
      return;
    }
    let html = '<table style="width:100%;border-collapse:collapse;">';
    html += '<thead><tr style="background:#f8fafc;border-bottom:2px solid #e2e8f0;">';
    html += '<th style="padding:12px;text-align:left;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Timestamp (ET)</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Total</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Passed</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Failed</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Unclear</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Pass %</th>';
    html += '<th style="padding:12px;text-align:center;font-size:0.75rem;font-weight:700;text-transform:uppercase;color:#64748b;">Actions</th>';
    html += '</tr></thead><tbody>';

    data.forEach(run => {
      const ts = run.ts;
      const formatted = formatTimestamp(ts);
      const passPct = run.total > 0 ? Math.round((run.passed / run.total) * 100) : 0;
      const passColor = passPct === 100 ? '#16a34a' : passPct >= 80 ? '#f59e0b' : '#dc2626';
      html += `<tr style="border-bottom:1px solid #f1f5f9;">
        <td style="padding:12px;font-size:0.88rem;color:#1e293b;">${formatted}</td>
        <td style="padding:12px;text-align:center;font-size:0.88rem;color:#475569;">${run.total}</td>
        <td style="padding:12px;text-align:center;font-size:0.88rem;color:#16a34a;font-weight:600;">${run.passed}</td>
        <td style="padding:12px;text-align:center;font-size:0.88rem;color:#dc2626;font-weight:600;">${run.failed}</td>
        <td style="padding:12px;text-align:center;font-size:0.88rem;color:#b45309;font-weight:600;">${run.unclear}</td>
        <td style="padding:12px;text-align:center;font-size:0.88rem;font-weight:700;color:${passColor};">${passPct}%</td>
        <td style="padding:12px;text-align:center;font-size:0.85rem;">
          ${run.has_report ? `<button class="btn btn-primary" style="padding:4px 10px;font-size:0.75rem;margin-right:4px;" onclick="location.href='/api/report/${run.stem}'">View</button>` : ''}
          ${run.has_report ? `<button class="btn btn-outline" style="padding:4px 10px;font-size:0.75rem;background:#f3f4f6;color:#374151;border:1px solid #d1d5db;" onclick="location.href='/api/report/${run.stem}?dl=1'">Download</button>` : '<span style="color:#94a3b8">No report</span>'}
        </td>
      </tr>`;
    });
    html += '</tbody></table>';
    historyList.innerHTML = html;
  } catch(e) {
    historyList.innerHTML = '<p style="color:#dc2626;padding:20px;text-align:center;">Failed to load reports: ' + e.message + '</p>';
  }
}

function formatTimestamp(stem) {
  const match = String(stem).match(/(\\d{4})(\\d{2})(\\d{2})_?(\\d{2})(\\d{2})(\\d{2})?/);
  if (!match) return stem || '';
  const year = parseInt(match[1], 10);
  const month = parseInt(match[2], 10);
  const day = parseInt(match[3], 10);
  const hour = parseInt(match[4], 10);
  const min = parseInt(match[5], 10);
  const sec = parseInt(match[6] || '0', 10);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(month)}/${pad(day)}/${year} ${pad(hour)}:${pad(min)}:${pad(sec)} ET`;
}

function formatDisplayTimestamp(value) {
  const raw = String(value || '').trim();
  if (!raw) return '';
  if (/^\\d{8}_?\\d{4,6}$/.test(raw)) return formatTimestamp(raw);
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) return raw;
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: 'America/New_York',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).formatToParts(d);
  const pick = type => parts.find(p => p.type === type)?.value || '';
  return `${pick('month')}/${pick('day')}/${pick('year')} ${pick('hour')}:${pick('minute')}:${pick('second')} ET`;
}

function _timestampSortValue(value) {
  const raw = String(value || '').trim();
  if (!raw) return 0;
  const stem = raw.match(/^(\\d{4})(\\d{2})(\\d{2})_?(\\d{2})(\\d{2})(\\d{2})?$/);
  if (stem) {
    return new Date(
      parseInt(stem[1], 10),
      parseInt(stem[2], 10) - 1,
      parseInt(stem[3], 10),
      parseInt(stem[4], 10),
      parseInt(stem[5], 10),
      parseInt(stem[6] || '0', 10),
    ).getTime();
  }
  const d = new Date(raw);
  return Number.isNaN(d.getTime()) ? 0 : d.getTime();
}

function _responseTimestamp(ts, fallback = '') {
  const val = ts || fallback || '';
  return val ? formatDisplayTimestamp(val) : '';
}

function _normalizeTranscriptTurns(test, runStem) {
  const startedAt = test.started_at || runStem;
  const finishedAt = test.finished_at || runStem;
  const transcript = Array.isArray(test.transcript) ? test.transcript : [];
  if (transcript.length) {
    return transcript.map((turn, idx) => ({
      turn: turn.turn || idx + 1,
      user: turn.user || '',
      responses: Array.isArray(turn.responses) ? turn.responses : [],
      started_at: turn.started_at || startedAt,
      finished_at: turn.finished_at || finishedAt,
    }));
  }

  const tasks = Array.isArray(test.tasks_sent) ? test.tasks_sent : [];
  const resps = Array.isArray(test.responses) ? test.responses : [];
  if (!tasks.length && !resps.length) return [];
  if (tasks.length <= 1 && resps.length <= 1) {
    return [{
      turn: 1,
      user: tasks.join('\n'),
      responses: resps,
      started_at: startedAt,
      finished_at: finishedAt,
    }];
  }
  return tasks.map((task, idx) => ({
    turn: idx + 1,
    user: task,
    responses: resps[idx] ? [resps[idx]] : [],
    started_at: startedAt,
    finished_at: finishedAt,
  }));
}

function _renderTranscriptBlock(turn) {
  const userTs = _responseTimestamp(turn.started_at);
  const respTs = _responseTimestamp(turn.finished_at, turn.started_at);
  const responses = (turn.responses || []).map((rsp, idx) => `
    <div style="background:#f8fafc;border-left:4px solid #94a3b8;padding:10px 12px;border-radius:8px;margin-bottom:6px;">
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:4px;">
        <div style="font-size:0.84rem;font-weight:700;color:#334155;">Asmi ${idx+1}</div>
        <div style="font-size:0.72rem;color:#64748b;font-family:monospace;">${esc(respTs)}</div>
      </div>
      <div style="color:#0f172a;font-size:0.9rem;white-space:pre-wrap;">${esc(rsp)}</div>
    </div>`).join('');
  return `<div style="border:1px solid #cbd5e1;border-radius:10px;padding:12px;margin-bottom:10px;background:#ffffff;">
    <div style="display:flex;gap:8px;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;">
      <div style="font-weight:700;color:#0f172a;">Turn ${turn.turn || ''}</div>
      <div style="font-size:0.72rem;color:#64748b;font-family:monospace;">${esc(userTs)}</div>
    </div>
    <div style="background:#eef2ff;border-left:4px solid #3b82f6;padding:10px 12px;border-radius:8px;margin-bottom:8px;">
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px;">
        <div style="font-size:0.84rem;font-weight:700;color:#1e3a8a;">You</div>
        <div style="font-size:0.72rem;color:#64748b;font-family:monospace;">${esc(userTs)}</div>
      </div>
      <div style="color:#0f172a;font-size:0.9rem;white-space:pre-wrap;">${esc(turn.user || '')}</div>
    </div>
    ${responses || '<div style="color:#64748b;font-size:0.85rem;">No responses recorded.</div>'}
  </div>`;
}

function _renderTranscriptCard(run, test) {
  const turns = _normalizeTranscriptTurns(test, run.stem || test.stem || '');
  const verdict = (test.verdict || 'UNCLEAR').toUpperCase();
  const vColor = verdict === 'PASS' ? '#166534' : verdict === 'FAIL' ? '#991b1b' : '#92400e';
  const vBg = verdict === 'PASS' ? '#dcfce7' : verdict === 'FAIL' ? '#fee2e2' : '#fef3c7';
  const ts = _responseTimestamp(test.started_at || run.stem || test.stem || '');
  return `<div style="border:1px solid #e2e8f0;border-radius:12px;padding:14px;background:#fff;">
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:10px;flex-wrap:wrap;">
      <span style="font-weight:700;color:#0f172a;">${esc(test.id)}: ${esc(test.name)}</span>
      <span style="background:${vBg};color:${vColor};padding:4px 8px;border-radius:999px;font-size:0.75rem;">${verdict}</span>
      <span style="color:#64748b;font-size:0.78rem;font-family:monospace;">${esc(ts)}</span>
      <span style="background:#e0f2fe;color:#0369a1;padding:4px 8px;border-radius:999px;font-size:0.75rem;">${turns.length} turn(s)</span>
    </div>
    ${turns.length ? turns.map(_renderTranscriptBlock).join('') : '<div style="color:#64748b;font-size:0.85rem;">No transcript available.</div>'}
    ${test.reason ? `<div style="margin-top:8px;font-size:0.85rem;color:#475569;"><strong>Judge:</strong> ${esc(test.reason)}</div>` : ''}
  </div>`;
}

function _flattenConversationRows(data) {
  const rows = [];
  (data || []).forEach(run => {
    (run.tests || []).forEach(test => {
      const turns = _normalizeTranscriptTurns(test, run.stem || '');
      if (!turns.length) {
        if ((test.tasks_sent || []).length) {
          rows.push({
            ts: test.started_at || run.stem || '',
            runStem: run.stem || '',
            testId: test.id || '',
            testName: test.name || '',
            side: 'you',
            text: (test.tasks_sent || []).join('\n'),
          });
        }
        if ((test.responses || []).length) {
          rows.push({
            ts: test.finished_at || test.started_at || run.stem || '',
            runStem: run.stem || '',
            testId: test.id || '',
            testName: test.name || '',
            side: 'asmi',
            text: (test.responses || []).join('\n'),
          });
        }
        return;
      }
      turns.forEach(turn => {
        rows.push({
          ts: turn.started_at || test.started_at || run.stem || '',
          runStem: run.stem || '',
          testId: test.id || '',
          testName: test.name || '',
          side: 'you',
          text: turn.user || '',
        });
        (turn.responses || []).forEach(rsp => {
          rows.push({
            ts: turn.finished_at || turn.started_at || test.finished_at || run.stem || '',
            runStem: run.stem || '',
            testId: test.id || '',
            testName: test.name || '',
            side: 'asmi',
            text: rsp || '',
          });
        });
      });
    });
  });
  rows.sort((a, b) => {
    const ta = _timestampSortValue(a.ts || 0);
    const tb = _timestampSortValue(b.ts || 0);
    return tb - ta;
  });
  return rows;
}

async function loadResponses() {
  const responsesList = document.getElementById('responsesList');
  responsesList.innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">Loading…</p>';
  const transcriptBtn = document.getElementById('responsesViewTranscriptBtn');
  const historyBtn = document.getElementById('responsesViewHistoryBtn');
  if (transcriptBtn && historyBtn) {
    transcriptBtn.style.background = _responsesViewMode === 'transcript' ? '#e0e7ff' : '#ffffff';
    transcriptBtn.style.color = _responsesViewMode === 'transcript' ? '#3730a3' : '#0f172a';
    historyBtn.style.background = _responsesViewMode === 'history' ? '#e0e7ff' : '#ffffff';
    historyBtn.style.color = _responsesViewMode === 'history' ? '#3730a3' : '#0f172a';
  }
  try {
    const res = await fetch('/api/responses');
    const data = await res.json();
    if (!data || !data.length) {
      responsesList.innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">No responses yet.</p>';
      return;
    }
    if (_responsesViewMode === 'history') {
      const rows = _flattenConversationRows(data);
      if (!rows.length) {
        responsesList.innerHTML = '<p style="color:#94a3b8;padding:20px;text-align:center;">No conversation rows yet.</p>';
        return;
      }
      responsesList.innerHTML = `
        <div style="border:1px solid #e2e8f0;border-radius:14px;padding:18px;background:#f8fafc;">
          <div style="font-size:0.95rem;font-weight:700;color:#0f172a;margin-bottom:14px;">All conversations</div>
          <div style="display:grid;gap:10px;">
            ${rows.map(row => `
              <div style="background:#ffffff;border:1px solid #e2e8f0;border-radius:10px;padding:12px;">
                <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:6px;">
                  <span style="font-size:0.72rem;color:#64748b;font-family:monospace;">${esc(formatDisplayTimestamp(row.ts || ''))}</span>
                  <span style="font-size:0.72rem;font-weight:700;color:#334155;background:#e2e8f0;padding:2px 8px;border-radius:999px;">${esc(row.runStem || '')}</span>
                  <span style="font-size:0.72rem;color:#475569;">${esc(row.testId || '')} · ${esc(row.testName || '')}</span>
                  <span style="font-size:0.72rem;font-weight:700;color:${row.side === 'you' ? '#1d4ed8' : '#15803d'};background:${row.side === 'you' ? '#dbeafe' : '#dcfce7'};padding:2px 8px;border-radius:999px;">${row.side === 'you' ? 'You' : 'Asmi'}</span>
                </div>
                <div style="color:#0f172a;font-size:0.9rem;white-space:pre-wrap;line-height:1.55;">${esc(row.text || '')}</div>
              </div>
            `).join('')}
          </div>
        </div>`;
      return;
    }

    let html = '';
    data.forEach(run => {
      const ts = formatTimestamp(run.stem);
      const runTests = (run.tests || []).map(test => _renderTranscriptCard(run, test)).join('');
      html += `<div style="border:1px solid #e2e8f0;border-radius:14px;padding:18px;background:#f8fafc;">
        <div style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:14px;">
          <div style="font-size:0.95rem;font-weight:700;color:#0f172a;">Run ${ts}</div>
          <div style="background:#e2e8f0;color:#1d4ed8;padding:4px 10px;border-radius:999px;font-size:0.78rem;">${run.tests.length} test${run.tests.length===1?'':'s'}</div>
          <div style="background:#d1fae5;color:#15803d;padding:4px 10px;border-radius:999px;font-size:0.78rem;">${run.totalResponses} total responses</div>
        </div>
        <div style="display:grid;gap:12px;">${runTests}</div>
      </div>`;
    });
    responsesList.innerHTML = html;
  } catch(e) {
    responsesList.innerHTML = '<p style="color:#dc2626;padding:20px;text-align:center;">Failed to load responses: ' + esc(e.message) + '</p>';
  }
}

async function loadAnalysis() {
  const summaryEl = document.getElementById('analysisSummary');
  const listEl = document.getElementById('analysisList');
  summaryEl.innerHTML = '<p style="color:#94a3b8;padding:12px 0;">Loading analysis…</p>';
  listEl.innerHTML = '';
  try {
    const res = await fetch('/api/analysis');
    const data = await res.json();
    const s = data.summary || {total:0, passed:0, failed:0, unclear:0, pass_rate:0};
    summaryEl.innerHTML = `
      <div style="display:flex;gap:10px;flex-wrap:wrap;">
        <div class="count" style="margin-left:0;">${s.total} total</div>
        <div class="count" style="margin-left:0;background:#dcfce7;color:#166534;">${s.passed} passed</div>
        <div class="count" style="margin-left:0;background:#fee2e2;color:#991b1b;">${s.failed} failed</div>
        <div class="count" style="margin-left:0;background:#fef3c7;color:#92400e;">${s.unclear} unclear</div>
        <div class="count" style="margin-left:0;background:#e0e7ff;color:#3730a3;">${s.pass_rate}% pass rate</div>
      </div>
      <div style="background:#0f172a;color:#e2e8f0;border-radius:8px;padding:14px;font-family:monospace;font-size:0.8rem;white-space:pre-wrap;line-height:1.6;border:1px solid #1e293b;">${esc(data.overall_analysis || 'No overall analysis yet.')}</div>
    `;

    const tests = data.tests || [];
    if (!tests.length) {
      listEl.innerHTML = '<p style="color:#94a3b8;padding:12px 0;">No per-test analysis yet.</p>';
      return;
    }

    listEl.innerHTML = tests.map(t => {
      const verdict = (t.verdict || 'UNCLEAR').toUpperCase();
      const vColor = verdict === 'PASS' ? '#166534' : verdict === 'FAIL' ? '#991b1b' : '#92400e';
      const vBg = verdict === 'PASS' ? '#dcfce7' : verdict === 'FAIL' ? '#fee2e2' : '#fef3c7';
      return `<div style="border:1px solid #e2e8f0;border-radius:12px;padding:12px;background:#fff;">
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
          <span style="font-family:monospace;color:#64748b;font-size:0.78rem;">${esc(formatTimestamp(t.stem || ''))}</span>
          <span style="background:${vBg};color:${vColor};padding:2px 8px;border-radius:999px;font-size:0.75rem;font-weight:700;">${verdict}</span>
          <span style="font-weight:700;color:#0f172a;">${esc(t.id || '')}</span>
          <span style="color:#334155;">${esc(t.name || '')}</span>
          <span style="color:#64748b;font-size:0.78rem;">${t.tasks_sent_count || 0} sent · ${t.responses_count || 0} responses</span>
        </div>
        <div style="color:#475569;font-size:0.85rem;line-height:1.55;">${esc(t.reason || '') || 'No judge reason.'}</div>
      </div>`;
    }).join('');
  } catch(e) {
    summaryEl.innerHTML = '<p style="color:#dc2626;padding:12px 0;">Failed to load analysis: ' + esc(e.message) + '</p>';
    listEl.innerHTML = '';
  }
}

load();
_startHistoryAutoRefresh();
</script>
</body>
</html>
"""


# ── HTTP server ────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        global _pending_run, _last_heartbeat, _stop_requested
        path = urlparse(self.path).path
        if path == "/api/tests":
            try:
                cases = load_test_cases()
                self._json(cases)
            except Exception as e:
                self._json({"error": str(e)})
        elif path == "/api/poll":
            _last_heartbeat = time.time()
            run = _pending_run
            stop = _stop_requested
            _stop_requested = False
            self._json({"run": run, "stop": stop})
        elif path == "/api/output":
            self._json({
                "status":   _run_status,
                "output":   _run_output,
                "results":  _run_results,
                "elapsed":  int(time.time() - _run_started) if _run_started else 0,
                "progress": _run_progress,
            })
        elif path == "/api/progress":
            self._json(_run_progress)
        elif path == "/health":
            self._json({"ok": True, "github": USE_GITHUB, "repo": GH_REPO})
        elif path == "/api/history":
            files = sorted(
                glob.glob(os.path.join(REPORTS_DIR, "results_*.json")),
                key=lambda p: _stem_sort_value(os.path.basename(p).replace("results_", "").replace(".json", "")),
                reverse=True,
            )
            history = []
            for f in files:
                stem = os.path.basename(f).replace("results_", "").replace(".json", "")
                try:
                    with open(f) as fp:
                        data = json.load(fp)
                    passed  = sum(1 for r in data if r.get("verdict") == "PASS")
                    failed  = sum(1 for r in data if r.get("verdict") == "FAIL")
                    unclear = sum(1 for r in data if r.get("verdict") == "UNCLEAR")
                    history.append({
                        "stem":    stem,
                        "ts":      stem,
                        "total":   len(data),
                        "passed":  passed,
                        "failed":  failed,
                        "unclear": unclear,
                        "has_report": os.path.exists(os.path.join(REPORTS_DIR, f"report_{stem}.html")),
                    })
                except Exception:
                    pass
            if _run_results and _run_result_stem and not any(r.get("stem") == _run_result_stem for r in history):
                passed  = sum(1 for r in _run_results if r.get("verdict") == "PASS")
                failed  = sum(1 for r in _run_results if r.get("verdict") == "FAIL")
                unclear = sum(1 for r in _run_results if r.get("verdict") == "UNCLEAR")
                history.insert(0, {
                    "stem": _run_result_stem,
                    "ts": _run_result_stem,
                    "total": len(_run_results),
                    "passed": passed,
                    "failed": failed,
                    "unclear": unclear,
                    "has_report": os.path.exists(os.path.join(REPORTS_DIR, f"report_{_run_result_stem}.html")),
                })
            self._json(history)
        elif path == "/api/responses":
            files = sorted(
                glob.glob(os.path.join(REPORTS_DIR, "results_*.json")),
                key=lambda p: _stem_sort_value(os.path.basename(p).replace("results_", "").replace(".json", "")),
                reverse=True,
            )
            responses = []
            for f in files:
                stem = os.path.basename(f).replace("results_", "").replace(".json", "")
                try:
                    with open(f) as fp:
                        data = json.load(fp)
                    tests = []
                    total_responses = 0
                    for r in data:
                        tests.append({
                            "id": r.get("id"),
                            "name": r.get("name"),
                            "category": r.get("category"),
                            "verdict": r.get("verdict"),
                            "reason": r.get("reason"),
                            "started_at": r.get("started_at"),
                            "finished_at": r.get("finished_at"),
                            "tasks_sent": r.get("tasks_sent", []),
                            "responses": r.get("responses", []),
                            "transcript": r.get("transcript", []),
                        })
                        total_responses += len(r.get("responses", []))
                    responses.append({
                        "stem": stem,
                        "tests": tests,
                        "totalResponses": total_responses,
                    })
                except Exception:
                    pass
            if _run_results and _run_result_stem and not any(r.get("stem") == _run_result_stem for r in responses):
                tests = []
                total_responses = 0
                for r in _run_results:
                    tests.append({
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "category": r.get("category"),
                        "verdict": r.get("verdict"),
                        "reason": r.get("reason"),
                        "started_at": r.get("started_at"),
                        "finished_at": r.get("finished_at"),
                        "tasks_sent": r.get("tasks_sent", []),
                        "responses": r.get("responses", []),
                        "transcript": r.get("transcript", []),
                    })
                    total_responses += len(r.get("responses", []))
                responses.insert(0, {
                    "stem": _run_result_stem,
                    "tests": tests,
                    "totalResponses": total_responses,
                })
            self._json(responses)
        elif path == "/api/analysis":
            self._json(_build_analysis_payload())
        elif path.startswith("/api/report/"):
            stem     = path.removeprefix("/api/report/")
            filepath = os.path.join(REPORTS_DIR, f"report_{stem}.html")
            qs       = urlparse(self.path).query
            download = "dl=1" in qs
            if not os.path.exists(filepath):
                self.send_response(404)
                self.end_headers()
                return
            with open(filepath, "rb") as fp:
                content = fp.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if download:
                self.send_header("Content-Disposition", f'attachment; filename="report_{stem}.html"')
            self.end_headers()
            self.wfile.write(content)
        else:
            self._html(HTML)

    def do_POST(self):
        global _pending_run, _last_heartbeat, _run_output, _run_status, _run_started, _run_report_html, _run_results, _run_result_stem, _stop_requested, _run_progress
        path = urlparse(self.path).path
        if path == "/api/tests":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                cases = json.loads(body)
                save_test_cases(cases)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/run":
            global _pending_run, _run_output, _run_status, _run_started, _run_results, _run_result_stem
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body) if length else {}
            except Exception:
                data = {}
            ids = data.get("ids")
            if isinstance(ids, list):
                ids = [str(x).strip() for x in ids if str(x).strip()]
            else:
                ids = None
            rid = (str(data.get("id")).strip() if data.get("id") is not None else None) or None
            cat = (str(data.get("category")).strip() if data.get("category") is not None else None) or None
            cats = data.get("categories")
            if isinstance(cats, list):
                cats = [str(x).strip() for x in cats if str(x).strip()]
            else:
                cats = None
            if not (ids or rid or cat or cats):
                self._json({"ok": False, "error": "No tests selected"})
                return
            _pending_run = {
                "category": cat,
                "categories": cats,
                "id":       rid,
                "ids":      ids,
                "interactive_auto_continue": bool(data.get("interactive_auto_continue", True)),
                "ts":       time.time(),
            }
            _run_output  = ""
            _run_status  = "running"
            _run_started = time.time()
            _run_results = []
            _run_result_stem = ""
            mac_online = (time.time() - _last_heartbeat) < 90
            self._json({"ok": True, "mac_online": mac_online})
        elif path == "/api/output":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body)
                _run_output      = data.get("output", "")
                _run_status      = data.get("status", "done")
                _run_report_html = data.get("report_html", "")
                _run_results     = data.get("results", [])
                m = re.search(r"Raw results:\s*\S*results_(\d{8}_?\d{4,6})\.json", _run_output)
                _run_result_stem = m.group(1) if m else _et_now_str("%Y%m%d_%H%M%S")
                if _run_status == "done" and _run_results:
                    _persist_run_artifacts(_run_result_stem, _run_results, _run_report_html)
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/generate":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body)
                prompt = data.get("prompt", "")
                count = int(data.get("count", 3))
                cases = generate_test_cases(prompt, count)
                self._json({"ok": True, "cases": cases})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/analyze":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data    = json.loads(body)
                results = data.get("results", [])
                analysis = analyze_behavior(results)
                self._json({"ok": True, "analysis": analysis["text"]})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/save-behavior":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data    = json.loads(body)
                content = data.get("content", "")
                path_md = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ASMI_BEHAVIOR_ANALYSIS.md")
                header  = f"\n\n---\n## Analysis — {_et_now_str('%Y-%m-%d %H:%M ET')}\n\n"
                with open(path_md, "a") as f:
                    f.write(header + content + "\n")
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/progress":
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body)
                _run_progress = {
                    "current_test": data.get("current_test"),
                    "current_category": data.get("current_category"),
                    "completed": data.get("completed", 0),
                    "total": data.get("total", 0),
                }
                self._json({"ok": True})
            except Exception as e:
                self._json({"ok": False, "error": str(e)})
        elif path == "/api/stop":
            _stop_requested = True
            _run_status     = "stopped"
            self._json({"ok": True})
        elif path == "/api/ack-run":
            _pending_run = None
            self._json({"ok": True})

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body.encode())

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    mode = f"GitHub ({GH_REPO}/{GH_FILE})" if USE_GITHUB else f"Local file ({LOCAL_FILE})"
    print(f"\n  Asmi Eval UI — http://localhost:{PORT}")
    print(f"  Storage: {mode}")
    print(f"  Ctrl+C to stop\n")
    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
