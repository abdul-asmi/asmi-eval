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
import urllib.request
import urllib.error
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse

PORT      = int(os.environ.get("PORT", 8765))
GH_TOKEN  = os.environ.get("GITHUB_TOKEN", "")
GH_REPO   = os.environ.get("GITHUB_REPO", "")
GH_FILE   = os.environ.get("GITHUB_FILE_PATH", "test_cases.py")

# Fallback: read/write local file if GitHub not configured (local dev)
LOCAL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_cases.py")
USE_GITHUB = bool(GH_TOKEN and GH_REPO)

CATEGORIES = [
    "sticky_message","call_dedup","call_summary","language_pref",
    "location_memory","onboarding","capability","threep_nudge",
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
<title>Asmi Eval — Test Case Editor</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
       background: #f1f5f9; color: #1e293b; }
header { background: #0f172a; color: white; padding: 16px 24px;
         display: flex; align-items: center; gap: 16px; }
header h1 { font-size: 1.1rem; font-weight: 600; }
header span { color: #94a3b8; font-size: 0.85rem; }
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
#toast { position: fixed; bottom: 24px; right: 24px; background: #1e293b; color: white;
         padding: 12px 20px; border-radius: 8px; font-size: 0.85rem; font-weight: 500;
         opacity: 0; transition: opacity .3s; pointer-events: none; z-index: 999; }
#toast.show { opacity: 1; }
.new-form { background: white; border-radius: 12px; padding: 20px;
            margin-bottom: 20px; box-shadow: 0 1px 4px rgba(0,0,0,.06);
            display: none; border: 2px dashed #3b82f6; }
.new-form.open { display: block; }
.saving { opacity: 0.6; pointer-events: none; }
</style>
</head>
<body>

<header>
  <h1>🧪 Asmi Eval — Test Case Editor</h1>
  <span id="subtitle">Loading…</span>
</header>

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
  <button class="btn btn-success" id="saveBtn" onclick="saveAll()">💾 Save All</button>
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

  <div id="testList"></div>
</main>

<div id="toast"></div>

<script>
let tests = [];

async function load() {
  document.getElementById('subtitle').textContent = 'Loading from GitHub…';
  try {
    const res = await fetch('/api/tests');
    tests = await res.json();
    if (tests.error) throw new Error(tests.error);
    render();
  } catch(e) {
    document.getElementById('subtitle').textContent = '❌ Load failed: ' + e.message;
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
  document.getElementById('subtitle').textContent   = `${tests.length} tests · GitHub`;

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
    html += `<div class="cat-section"><div class="cat-label">${CAT_LABELS[cat]||cat} (${items.length})</div>`;
    items.forEach(t => { html += renderCard(t); });
    html += '</div>';
  }
  document.getElementById('testList').innerHTML = html || '<p style="color:#94a3b8;padding:20px">No tests match filter.</p>';
}

function renderCard(t) {
  const idx   = tests.indexOf(t);
  const msgs  = t.messages ? t.messages.join('\\n') : '';
  const pre   = t.precondition ? `<span class="badge badge-warn">⚠ precondition</span>` : '';
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
        <button class="btn btn-success" onclick="saveAll()">💾 Save to GitHub</button>
        <button class="btn btn-danger" onclick="deleteTest(${idx})">🗑 Delete</button>
      </div>
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

  if (!tc.id || !tc.name) { toast('❌ ID and Name are required'); return; }
  tests.push(tc);
  toggleNew();
  render();
  toast('✅ Test added — click Save All to commit to GitHub');
}

function deleteTest(idx) {
  if (!confirm('Delete this test case?')) return;
  tests.splice(idx, 1);
  render();
  toast('🗑 Deleted — click Save All to commit to GitHub');
}

async function saveAll() {
  const btn = document.getElementById('saveBtn');
  btn.textContent = '⏳ Saving…';
  btn.classList.add('saving');
  try {
    const res = await fetch('/api/tests', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(tests),
    });
    const data = await res.json();
    if (data.ok) toast('✅ Committed to GitHub — pull on Mac before next eval run');
    else toast('❌ Save failed: ' + data.error);
  } catch(e) {
    toast('❌ Save failed: ' + e.message);
  } finally {
    btn.textContent = '💾 Save All';
    btn.classList.remove('saving');
  }
}

function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 4000);
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
        path = urlparse(self.path).path
        if path == "/api/tests":
            try:
                cases = load_test_cases()
                self._json(cases)
            except Exception as e:
                self._json({"error": str(e)})
        elif path == "/health":
            self._json({"ok": True, "github": USE_GITHUB, "repo": GH_REPO})
        else:
            self._html(HTML)

    def do_POST(self):
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
