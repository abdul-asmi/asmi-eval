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
import json
import os
import re
import time
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

import google.genai as genai
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
_stop_requested  = False  # True when the user clicks Stop
_run_progress    = {}     # dict {current_test, current_category, completed, total}

PORT      = int(os.environ.get("PORT", 8765))
GH_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GH_REPO   = os.environ.get("GITHUB_REPO", "")
GH_FILE   = os.environ.get("GITHUB_FILE_PATH", "test_cases.py")

# Fallback: read/write local file if GitHub not configured (local dev)
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cases.py")
USE_GITHUB = bool(GH_TOKEN and GH_REPO)

CATEGORIES = [
    "sticky_message","call_dedup","call_summary","language_pref",
    "location_memory","onboarding","capability","threep_nudge","generated",
]
TYPES = ["single","burst","sequence","dedup","burst_with_setup"]


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
<title>Break me, Asmi</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f1f5f9; color: #1e293b; }
header { background: linear-gradient(135deg, #1e1b4b 0%, #312e81 55%, #4c1d95 100%);
         color: white; padding: 16px 24px;
         display: flex; align-items: center; gap: 16px; }
.logo { width:38px; height:38px; border-radius:10px; flex-shrink:0;
        background: linear-gradient(135deg,#c084fc,#7c3aed);
        display:flex; align-items:center; justify-content:center;
        box-shadow: 0 0 0 2px rgba(255,255,255,.15), 0 4px 12px rgba(124,58,237,.5); }
.logo svg { display:block; }
.header-text { display:flex; flex-direction:column; gap:2px; }
.header-title { font-size: 1.15rem; font-weight: 700; letter-spacing:-.01em; }
.header-sub { color: #c4b5fd; font-size: 0.82rem; }
.toolbar { background: white; border-bottom: 1px solid #e2e8f0;
           padding: 12px 24px; display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
.toolbar select, .toolbar input { padding: 6px 10px; border: 1px solid #e2e8f0;
                                   border-radius: 6px; font-size: 0.85rem; }
.btn { padding: 7px 16px; border-radius: 6px; border: none; cursor: pointer;
       font-size: 0.85rem; font-weight: 600; }
.btn-primary { background: #3b82f6; color: white; }
.btn-primary:hover { background: #2563eb; }
.btn-success { background: #22c55e; color: white; }
.btn-success:hover { background: #16a34a; }
.btn-danger  { background: #ef4444; color: white; }
.btn-danger:hover  { background: #dc2626; }
.btn-outline { background: white; color: #374151; border: 1px solid #d1d5db; }
.btn-outline:hover { background: #f9fafb; }
.count { background: #eff6ff; color: #1d4ed8; padding: 4px 10px;
         border-radius: 99px; font-size: 0.8rem; font-weight: 700; margin-left: auto; }
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
  <div class="logo">
    <svg width="22" height="22" viewBox="0 0 22 22" fill="none" xmlns="http://www.w3.org/2000/svg">
      <path d="M11 2L13.8 8.6H20.4L15 12.8L17.2 19.4L11 15.2L4.8 19.4L7 12.8L1.6 8.6H8.2L11 2Z"
            fill="white" fill-opacity="0.95"/>
    </svg>
  </div>
  <div class="header-text">
    <div class="header-title">Break me, Asmi</div>
    <div class="header-sub" id="subtitle">Ready when you are.</div>
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
  <input type="text" id="search" placeholder="Search tests…" style="width:180px" oninput="filter()">
  <select id="filterCat" onchange="filter()"><option value="">All categories</option></select>
  <select id="filterType" onchange="filter()">
    <option value="">All types</option>
    <option value="single">single</option>
    <option value="burst">burst</option>
    <option value="sequence">sequence</option>
    <option value="dedup">dedup</option>
    <option value="burst_with_setup">burst_with_setup</option>
  </select>
  <button class="btn btn-primary" onclick="toggleNew()">+ Add Test</button>
  <button class="btn btn-outline" onclick="toggleGenerate()" style="background:#f3f4f6;color:#374151;border:1px solid #d1d5db;">🤖 Generate</button>
  <button class="btn btn-success" id="saveBtn" onclick="saveAll()">💾 Save All</button>
  <select id="runCat" style="margin-left:12px"><option value="">All tests</option></select>
  <button class="btn btn-run" id="runBtn" onclick="runTests()">▶ Run</button>
  <span class="count" id="countBadge">0 tests</span>
</div>

<main>

  <!-- New test form -->
  <div class="new-form" id="newForm">
    <div style="font-weight:700;margin-bottom:14px;font-size:1rem;">New Test Case</div>
    <div class="form-grid">
      <div><label>ID</label><input type="text" id="new_id" placeholder="e.g. sticky_05"></div>
      <div><label>Name</label><input type="text" id="new_name" placeholder="Short description"></div>
      <div>
        <label>Category</label>
        <select id="new_category"></select>
      </div>
      <div>
        <label>Type</label>
        <select id="new_type" onchange="toggleNewMsgFields()">
          <option value="single">single</option>
          <option value="burst">burst</option>
          <option value="sequence">sequence</option>
          <option value="dedup">dedup</option>
          <option value="burst_with_setup">burst_with_setup</option>
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
      <div><label>Wait (seconds)</label><input type="text" id="new_wait" value="120"></div>
      <div><label>Expected Responses</label><input type="text" id="new_expected" placeholder="optional"></div>
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
        <select id="generate_category"><option value="">Any category</option></select>
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

  <div id="testList"></div>
</main>
<div id="toast"></div>
<script>
let tests = [];
let _editingId = null;

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
  personalization:    {label:'Personalization',    color:'#3730a3', bg:'#eef2ff'},
  reengagement:       {label:'Reengagement',       color:'#7e22ce', bg:'#faf5ff'},
  guardrails:         {label:'Guardrails',         color:'#991b1b', bg:'#fee2e2'},
  generated:          {label:'Generated',          color:'#334155', bg:'#f1f5f9'},
};
const CAT_ORDER = [
  'onboarding','capability','sticky_message','call_dedup','cadence_control',
  'call_summary','voicemail','task_reliability','task_specific_call','threep_nudge',
  'location_memory','language_pref','timezone','checklist','chat_brevity','chat_flow',
  'personalization','reengagement','guardrails','generated',
];

function _catOptions(selected = '', includeAll = false) {
  let html = includeAll ? '<option value="">All categories</option>' : '';
  CAT_ORDER.forEach(c => {
    const m = CAT_META[c] || {label:c};
    html += `<option value="${c}" ${selected===c?'selected':''}>${m.label}</option>`;
  });
  return html;
}

function _initCatDropdowns() {
  ['filterCat','runCat'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = _catOptions('', true);
  });
  ['new_category','generate_category'].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.innerHTML = _catOptions('');
  });
}

async function load() {
  _initCatDropdowns();
  document.getElementById('subtitle').textContent = 'Loading…';
  try {
    const res = await fetch('/api/tests');
    tests = await res.json();
    if (tests.error) throw new Error(tests.error);
    render();
  } catch(e) {
    document.getElementById('subtitle').textContent = 'Load failed: ' + e.message;
    document.getElementById('testList').innerHTML = '<p style="color:#ef4444;padding:20px">Failed to load test cases. Check GitHub env vars.</p>';
  }
}

function render() {
  const search = document.getElementById('search').value.toLowerCase();
  const catF   = document.getElementById('filterCat').value;
  const typeF  = document.getElementById('filterType').value;

  const filtered = tests.filter(t =>
    (!search || t.id.includes(search) || t.name.toLowerCase().includes(search) ||
     (t.message||'').toLowerCase().includes(search) ||
     (t.pass_criteria||'').toLowerCase().includes(search)) &&
    (!catF  || t.category === catF) &&
    (!typeF || t.type === typeF)
  );

  document.getElementById('countBadge').textContent = `${filtered.length} / ${tests.length} tests`;
  document.getElementById('subtitle').textContent   = `${tests.length} tests`;

  const bycat = {};
  filtered.forEach(t => {
    if (!bycat[t.category]) bycat[t.category] = [];
    bycat[t.category].push(t);
  });

  const orderedCats = CAT_ORDER.filter(c => bycat[c])
    .concat(Object.keys(bycat).filter(c => !CAT_ORDER.includes(c)));

  if (!orderedCats.length) {
    document.getElementById('testList').innerHTML = '<p style="color:#94a3b8;padding:20px">No tests match filter.</p>';
    return;
  }

  let rows = '';
  orderedCats.forEach(cat => {
    const items = bycat[cat];
    const m = CAT_META[cat] || {label:cat, color:'#334155', bg:'#f8fafc'};
    rows += `<tr class="cat-row">
      <td colspan="5" style="color:${m.color}">
        <span style="background:${m.bg};padding:2px 10px;border-radius:99px">${m.label}</span>
        <span style="color:#94a3b8;font-weight:400;margin-left:6px">${items.length} test${items.length!==1?'s':''}</span>
      </td>
      <td style="text-align:right">
        <button class="run-cell-btn" style="font-size:0.7rem;padding:3px 10px"
          onclick="runByCategory('${cat}')">▶ Run</button>
      </td>
    </tr>`;
    items.forEach(t => { rows += renderRow(t); });
  });

  document.getElementById('testList').innerHTML =
    `<table class="test-table">
      <thead><tr>
        <th style="width:44px"></th>
        <th style="width:110px">ID</th>
        <th>Name</th>
        <th style="width:100px">Type</th>
        <th>Message preview</th>
        <th style="width:68px"></th>
      </tr></thead>
      <tbody>${rows}</tbody>
    </table>`;
}

function renderRow(t) {
  const idx      = tests.indexOf(t);
  const m        = CAT_META[t.category] || {label:t.category, color:'#334155', bg:'#f8fafc'};
  const msgs     = t.messages ? t.messages.join('\\n') : '';
  const preview  = esc((t.message || (t.messages||[]).join(' · ') || '').substring(0, 80));
  const preWarn  = t.precondition ? ' ⚠' : '';

  return `
  <tr class="test-row" id="row_${t.id}" onclick="editRow('${t.id}')">
    <td onclick="event.stopPropagation()">
      <button class="run-cell-btn" id="runbtn_${t.id}" onclick="runById('${t.id}')">▶</button>
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
  <tr class="edit-row" id="editrow_${t.id}" style="display:none">
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
              ${['single','burst','sequence','dedup','burst_with_setup']
                .map(tp=>`<option value="${tp}" ${t.type===tp?'selected':''}>${tp}</option>`).join('')}
            </select>
          </div>
          ${t.message !== undefined ? `
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

function update(idx, key, val) {
  if (val === undefined || val === '') delete tests[idx][key];
  else tests[idx][key] = val;
}

function updateMsgs(idx, val) {
  tests[idx].messages = val.split('\\n').map(s=>s.trim()).filter(Boolean);
}

function toggleNew() {
  document.getElementById('newForm').classList.toggle('open');
}

function toggleNewMsgFields() {
  const type = document.getElementById('new_type').value;
  const multi = ['burst','sequence','burst_with_setup'].includes(type);
  document.getElementById('new_msg_wrap').style.display  = multi ? 'none' : '';
  document.getElementById('new_msgs_wrap').style.display = multi ? '' : 'none';
}

function addNew() {
  const type = document.getElementById('new_type').value;
  const tc = {
    id:           document.getElementById('new_id').value.trim(),
    name:         document.getElementById('new_name').value.trim(),
    category:     document.getElementById('new_category').value,
    type:         type,
    wait:         parseInt(document.getElementById('new_wait').value) || 120,
    pass_criteria: document.getElementById('new_pass_criteria').value.trim(),
  };
  if (['burst','sequence','burst_with_setup'].includes(type)) {
    tc.messages = document.getElementById('new_messages').value.split('\\n').map(s=>s.trim()).filter(Boolean);
    const exp = parseInt(document.getElementById('new_expected').value);
    if (exp) tc.expected_responses = exp;
  } else {
    tc.message = document.getElementById('new_message').value.trim();
  }
  const pre  = document.getElementById('new_precondition').value.trim();
  const mchk = document.getElementById('new_manual_check').value.trim();
  if (pre)  tc.precondition  = pre;
  if (mchk) tc.manual_check  = mchk;

  if (!tc.id || !tc.name) { alert('ID and Name are required'); return; }
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
  btn.textContent = 'Saving…';
  btn.classList.add('saving');
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
    btn.textContent = 'Save All';
    btn.classList.remove('saving');
  }
}

let _pollTimer      = null;
let _runStart       = 0;
let _activeTestId   = null;
let _lastRunAnalysis = '';

async function runTests() {
  const cat = document.getElementById('runCat').value;
  await _triggerRun({category: cat || null});
}

async function runById(id) {
  _activeTestId = id;
  // Open the edit row so inline result is visible
  editRow(id);
  // Show running indicator inline
  const inlineEl = document.getElementById('result_' + id);
  if (inlineEl) {
    inlineEl.className = 'inline-result running';
    inlineEl.innerHTML = '<span class="inline-running-dots">Running</span>';
  }
  const btn = document.getElementById('runbtn_' + id);
  if (btn) btn.disabled = true;
  await _triggerRun({id});
}

async function runByCategory(cat) {
  await _triggerRun({category: cat});
}

async function _triggerRun(payload) {
  const label = payload.id ? `test: ${payload.id}` :
                payload.category ? `category: ${payload.category}` : 'all tests';
  try {
    const res = await fetch('/api/run', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    const data = await res.json();
    if (data.mac_online) {
      toast(`Running ${label}…`);
      _openOutput(`Running ${label}…`);
    } else {
      toast('Mac is offline — daemon not running');
    }
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
        // Multi-test: show full results panel
        if (!_activeTestId) {
          document.getElementById('outputPanel').style.display = 'block';
          document.getElementById('outputStatus').textContent = 'Done';
          document.getElementById('outputElapsed').textContent = `${secs2}s elapsed`;
          _renderResults(data.results);
          if (data.results.length > 1) _runBehaviorAnalysis(data.results);
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
    }
  } catch(e) {}
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

  return `<div class="rt-card ${vcls}">
    <div class="rt-card-hdr">
      <span class="rt-tid">[${esc(r.id||'')}]</span>
      <span class="rt-tname">${esc(r.name||'')}</span>
      <span class="rt-badge ${vcls}">${v}</span>
      ${dur ? `<span class="rt-dur">${dur}</span>` : ''}
    </div>
    ${tasks ? `<div class="rt-section"><div class="rt-slabel">Task sent</div>${tasks}</div>` : ''}
    ${resps ? `<div class="rt-section"><div class="rt-slabel">Responses (${(r.responses||[]).length})</div>${resps}</div>` : ''}
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
  if (!data.mac_online) document.getElementById('genRunStatus').textContent = '⚠ Daemon offline — waiting…';

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

load();
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
            # Daemon calls this: update heartbeat, return + clear any pending run
            _last_heartbeat = time.time()
            run = _pending_run
            _pending_run = None
            stop = _stop_requested
            _stop_requested = False  # clear after daemon picks it up
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
        else:
            self._html(HTML)

    def do_POST(self):
        global _pending_run, _last_heartbeat, _run_output, _run_status, _run_started, _run_report_html, _run_results, _stop_requested, _run_progress
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
            length = int(self.headers.get("Content-Length", 0))
            body   = self.rfile.read(length)
            try:
                data = json.loads(body) if length else {}
            except Exception:
                data = {}
            _pending_run = {
                "category": data.get("category"),
                "id":       data.get("id"),
                "ts":       time.time(),
            }
            _run_output  = ""
            _run_status  = "running"
            _run_started = time.time()
            _run_results = []
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
                header  = f"\n\n---\n## Analysis — {time.strftime('%Y-%m-%d %H:%M')}\n\n"
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

    def _html(self, body):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def _json(self, data):
        body = json.dumps(data, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
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
