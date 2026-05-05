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
main { padding: 24px; max-width: 1000px; margin: 0 auto; }
.card { background: white; border-radius: 12px; margin-bottom: 16px;
        box-shadow: 0 1px 4px rgba(0,0,0,.06); overflow: hidden; }
.card-header { padding: 14px 18px; display: flex; align-items: center; gap: 10px;
               cursor: pointer; border-bottom: 1px solid #f1f5f9; }
.card-header:hover { background: #f8fafc; }
.card-body { padding: 18px; display: none; }
.card-body.open { display: block; }
.badge { padding: 2px 8px; border-radius: 99px; font-size: 0.72rem; font-weight: 700; }
.badge-cat { background: #eff6ff; color: #1d4ed8; }
.badge-type { background: #f0fdf4; color: #15803d; }
.badge-warn { background: #fffbeb; color: #b45309; }
.test-name { font-weight: 600; flex: 1; font-size: 0.95rem; }
.test-id { color: #94a3b8; font-size: 0.8rem; font-family: monospace; }
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
.form-actions { margin-top: 16px; display: flex; gap: 8px; }
.cat-section { margin-bottom: 8px; }
.cat-label { font-size: 0.75rem; font-weight: 700; color: #64748b;
             text-transform: uppercase; letter-spacing: .06em;
             padding: 8px 0 4px; border-bottom: 1px solid #e2e8f0; margin-bottom: 8px; }
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
  <input type="text" id="search" placeholder="Search tests…" style="width:200px" oninput="filter()">
  <select id="filterCat" onchange="filter()">
    <option value="">All categories</option>
    <option value="sticky_message">Sticky Message</option>
    <option value="call_dedup">Call Deduping</option>
    <option value="call_summary">Post-Call Summary</option>
    <option value="language_pref">Language Preference</option>
    <option value="location_memory">Location Memory</option>
    <option value="onboarding">Onboarding</option>
    <option value="capability">Capability Prompts</option>
    <option value="threep_nudge">3P Call Nudge</option>
  </select>
  <select id="filterType" onchange="filter()">
    <option value="">All types</option>
    <option value="single">single</option>
    <option value="burst">burst</option>
    <option value="sequence">sequence</option>
    <option value="dedup">dedup</option>
    <option value="burst_with_setup">burst_with_setup</option>
  </select>
  <button class="btn btn-primary" onclick="toggleNew()">+ Add Test</button>
  <button class="btn btn-outline" onclick="toggleGenerate()" style="background:#f3f4f6;color:#374151;border:1px solid #d1d5db;">🤖 Generate Tests</button>
  <button class="btn btn-success" id="saveBtn" onclick="saveAll()">Save All</button>
  <select id="runCat" style="margin-left:12px">
    <option value="">All tests</option>
    <option value="sticky_message">Sticky Message</option>
    <option value="call_dedup">Call Deduping</option>
    <option value="call_summary">Post-Call Summary</option>
    <option value="language_pref">Language Preference</option>
    <option value="location_memory">Location Memory</option>
    <option value="onboarding">Onboarding</option>
    <option value="capability">Capability Prompts</option>
    <option value="threep_nudge">3P Call Nudge</option>
  </select>
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
        <select id="new_category">
          <option value="sticky_message">Sticky Message</option>
          <option value="call_dedup">Call Deduping</option>
          <option value="call_summary">Post-Call Summary</option>
          <option value="language_pref">Language Preference</option>
          <option value="location_memory">Location Memory</option>
          <option value="onboarding">Onboarding</option>
          <option value="capability">Capability Prompts</option>
          <option value="threep_nudge">3P Call Nudge</option>
        </select>
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
        <select id="generate_category">
          <option value="">Any category</option>
          <option value="sticky_message">Sticky Message</option>
          <option value="call_dedup">Call Deduping</option>
          <option value="call_summary">Post-Call Summary</option>
          <option value="language_pref">Language Preference</option>
          <option value="location_memory">Location Memory</option>
          <option value="onboarding">Onboarding</option>
          <option value="capability">Capability Prompts</option>
          <option value="threep_nudge">3P Call Nudge</option>
        </select>
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

async function load() {
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
  const search  = document.getElementById('search').value.toLowerCase();
  const catF    = document.getElementById('filterCat').value;
  const typeF   = document.getElementById('filterType').value;

  const filtered = tests.filter(t =>
    (!search || t.id.includes(search) || t.name.toLowerCase().includes(search) ||
     (t.message||'').toLowerCase().includes(search)) &&
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

  const CAT_LABELS = {
    sticky_message:'Sticky Message', call_dedup:'Call Deduping',
    call_summary:'Post-Call Summary', language_pref:'Language Preference',
    location_memory:'Location Memory', onboarding:'Onboarding',
    capability:'Capability Prompts', threep_nudge:'3P Call Nudge',
  };

  let html = '';
  for (const [cat, items] of Object.entries(bycat)) {
    html += `<div class="cat-section">
      <div class="cat-label" style="display:flex;align-items:center">
        <span>${CAT_LABELS[cat]||cat} (${items.length})</span>
        <button class="btn btn-run" style="margin-left:auto;padding:3px 12px;font-size:0.75rem"
          onclick="runByCategory('${cat}')">▶ Run category</button>
      </div>`;
    items.forEach(t => { html += renderCard(t); });
    html += '</div>';
  }
  document.getElementById('testList').innerHTML = html || '<p style="color:#94a3b8;padding:20px">No tests match filter.</p>';
}

function renderCard(t) {
  const idx   = tests.indexOf(t);
  const msgs  = t.messages ? t.messages.join('\\n') : '';
  const pre   = t.precondition ? `<span class="badge badge-warn">precondition</span>` : '';
  return `
  <div class="card" id="card_${t.id}">
    <div class="card-header" onclick="toggle('${t.id}')">
      <span class="test-id">[${t.id}]</span>
      <span class="test-name">${t.name}</span>
      ${pre}
      <span class="badge badge-cat">${t.category}</span>
      <span class="badge badge-type">${t.type}</span>
    </div>
    <div class="card-body" id="body_${t.id}">
      <div class="form-grid">
        <div><label>ID</label><input type="text" value="${esc(t.id)}" onchange="update(${idx},'id',this.value)"></div>
        <div><label>Name</label><input type="text" value="${esc(t.name)}" onchange="update(${idx},'name',this.value)"></div>
        <div>
          <label>Category</label>
          <select onchange="update(${idx},'category',this.value)">
            ${['sticky_message','call_dedup','call_summary','language_pref','location_memory','onboarding','capability','threep_nudge']
              .map(c=>`<option value="${c}" ${t.category===c?'selected':''}>${c}</option>`).join('')}
          </select>
        </div>
        <div>
          <label>Type</label>
          <select onchange="update(${idx},'type',this.value)">
            ${['single','burst','sequence','dedup','burst_with_setup']
              .map(tp=>`<option value="${tp}" ${t.type===tp?'selected':''}>${tp}</option>`).join('')}
          </select>
        </div>
        ${t.message !== undefined ? `
        <div class="form-full">
          <label>Message</label>
          <input type="text" value="${esc(t.message||'')}" onchange="update(${idx},'message',this.value)">
        </div>` : ''}
        ${t.messages !== undefined ? `
        <div class="form-full">
          <label>Messages (one per line)</label>
          <textarea onchange="updateMsgs(${idx},this.value)">${esc(msgs)}</textarea>
        </div>` : ''}
        <div><label>Wait (seconds)</label>
          <input type="text" value="${t.wait||120}" onchange="update(${idx},'wait',parseInt(this.value))">
        </div>
        <div><label>Expected Responses</label>
          <input type="text" value="${t.expected_responses||''}" onchange="update(${idx},'expected_responses',parseInt(this.value)||undefined)">
        </div>
        <div class="form-full"><label>Pass Criteria</label>
          <textarea onchange="update(${idx},'pass_criteria',this.value)">${esc(t.pass_criteria||'')}</textarea>
        </div>
        <div class="form-full"><label>Precondition (optional)</label>
          <input type="text" value="${esc(t.precondition||'')}" onchange="update(${idx},'precondition',this.value||undefined)">
        </div>
        <div class="form-full"><label>Manual Check (optional)</label>
          <input type="text" value="${esc(t.manual_check||'')}" onchange="update(${idx},'manual_check',this.value||undefined)">
        </div>
      </div>
      <div class="form-actions">
        <button class="btn btn-run" id="runbtn_${t.id}" onclick="runById('${t.id}')">▶ Run this test</button>
        <button class="btn btn-success" onclick="saveAll()">Save</button>
        <button class="btn btn-danger" onclick="deleteTest(${idx})">Delete</button>
      </div>
      <div class="inline-result" id="result_${t.id}"></div>
    </div>
  </div>`;
}

function esc(s) {
  return String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
}

function toggle(id) {
  document.getElementById('body_' + id).classList.toggle('open');
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
  // Open the card body so inline result is visible
  const body = document.getElementById('body_' + id);
  if (body && !body.classList.contains('open')) body.classList.add('open');
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
    } else if (data.status === 'done' || data.status === 'stopped') {
      clearInterval(_pollTimer);
      document.getElementById('stopBtn').style.display = 'none';
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
                "status":  _run_status,
                "output":  _run_output,
                "results": _run_results,
                "elapsed": int(time.time() - _run_started) if _run_started else 0,
            })
        elif path == "/health":
            self._json({"ok": True, "github": USE_GITHUB, "repo": GH_REPO})
        else:
            self._html(HTML)

    def do_POST(self):
        global _pending_run, _last_heartbeat, _run_output, _run_status, _run_started, _run_report_html, _run_results
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
        elif path == "/api/stop":
            global _stop_requested, _run_status
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
