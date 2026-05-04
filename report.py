# ─── HTML Report Generator ────────────────────────────────────────────────────

from datetime import datetime
from collections import defaultdict


_VERDICT_COLOR = {"PASS": "#22c55e", "FAIL": "#ef4444", "UNCLEAR": "#f59e0b"}
_VERDICT_BG    = {"PASS": "#f0fdf4", "FAIL": "#fef2f2", "UNCLEAR": "#fffbeb"}

CATEGORIES = {
    "sticky_message":  "Sticky Message",
    "call_dedup":      "Call Deduping",
    "call_summary":    "Post-Call Summary",
    "language_pref":   "Language Preference",
    "location_memory": "Location Memory",
    "onboarding":      "Onboarding Reactions",
    "capability":      "Capability Prompts",
    "threep_nudge":    "3P Call Nudge",
}


def generate(results: list[dict], output_path: str = "report.html"):
    total  = len(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    failed = sum(1 for r in results if r["verdict"] == "FAIL")
    other  = total - passed - failed

    by_cat = defaultdict(list)
    for r in results:
        by_cat[r["category"]].append(r)

    cat_rows = ""
    for cat_key, cat_results in by_cat.items():
        cat_label  = CATEGORIES.get(cat_key, cat_key)
        cat_pass   = sum(1 for r in cat_results if r["verdict"] == "PASS")
        cat_total  = len(cat_results)
        pct        = int(cat_pass / cat_total * 100) if cat_total else 0
        bar_color  = "#22c55e" if pct == 100 else "#ef4444" if pct < 50 else "#f59e0b"

        test_rows = ""
        for r in cat_results:
            v      = r["verdict"]
            color  = _VERDICT_COLOR.get(v, "#6b7280")
            bg     = _VERDICT_BG.get(v, "#f9fafb")
            tasks  = "<br>".join(f"<code>{t}</code>" for t in r.get("tasks_sent", []))
            resps  = ""
            for i, resp in enumerate(r.get("responses", [])):
                resps += f'<div class="resp">Response {i+1}: {resp or "<em>no response</em>"}</div>'

            manual = ""
            if r.get("manual_check"):
                manual = f'<div class="manual">📋 Manual check: {r["manual_check"]}</div>'
            note = ""
            if r.get("note"):
                note = f'<div class="note">📝 {r["note"]}</div>'
            pre = ""
            if r.get("precondition"):
                pre = f'<div class="precond">⚠ Precondition: {r["precondition"]}</div>'
            count_info = ""
            if r.get("count_verdict"):
                cv = r["count_verdict"]
                cv_color = _VERDICT_COLOR.get(cv["verdict"], "#6b7280")
                count_info = f'<span class="badge" style="background:{cv_color}22;color:{cv_color}">Count: {cv["verdict"]}</span> <small>{cv["reason"]}</small>'

            test_rows += f"""
            <details class="test-row" style="background:{bg};border-left:4px solid {color}">
                <summary>
                    <span class="badge" style="background:{color}22;color:{color}">{v}</span>
                    <strong>[{r["id"]}]</strong> {r["name"]}
                </summary>
                <div class="test-detail">
                    {pre}
                    <div class="section-label">Tasks sent</div>
                    <div class="tasks">{tasks}</div>
                    <div class="section-label">Responses ({len(r.get("responses",[]))})</div>
                    {resps if resps else '<em>No responses captured</em>'}
                    <div class="section-label">Judge</div>
                    <div class="reason">{r.get("reason","")}</div>
                    {count_info}
                    {manual}
                    {note}
                </div>
            </details>
            """

        cat_rows += f"""
        <div class="category">
            <div class="cat-header">
                <span class="cat-name">{cat_label}</span>
                <span class="cat-score">{cat_pass}/{cat_total}</span>
                <div class="bar-track">
                    <div class="bar-fill" style="width:{pct}%;background:{bar_color}"></div>
                </div>
            </div>
            {test_rows}
        </div>
        """

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M")
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Asmi Eval Report — {run_time}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f8fafc; color: #1e293b; padding: 24px; }}
  h1   {{ font-size: 1.5rem; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; font-size: 0.9rem; margin-bottom: 24px; }}

  .summary {{ display: flex; gap: 16px; margin-bottom: 32px; flex-wrap: wrap; }}
  .stat {{ background: white; border-radius: 12px; padding: 16px 24px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); min-width: 120px; text-align: center; }}
  .stat .num {{ font-size: 2rem; font-weight: 700; }}
  .stat .lbl {{ font-size: 0.8rem; color: #64748b; margin-top: 2px; }}

  .category {{ background: white; border-radius: 12px; margin-bottom: 20px;
               box-shadow: 0 1px 4px rgba(0,0,0,.08); overflow: hidden; }}
  .cat-header {{ display: flex; align-items: center; gap: 12px; padding: 14px 20px;
                 background: #f1f5f9; border-bottom: 1px solid #e2e8f0; }}
  .cat-name  {{ font-weight: 600; flex: 1; }}
  .cat-score {{ font-weight: 700; font-size: 1rem; min-width: 40px; text-align: right; }}
  .bar-track {{ flex: 2; height: 8px; background: #e2e8f0; border-radius: 99px; overflow: hidden; }}
  .bar-fill  {{ height: 100%; border-radius: 99px; transition: width .4s; }}

  details.test-row {{ margin: 8px 16px; border-radius: 8px; overflow: hidden; }}
  details.test-row summary {{
    padding: 10px 14px; cursor: pointer; list-style: none;
    display: flex; align-items: center; gap: 10px; font-size: 0.9rem;
  }}
  details.test-row summary::-webkit-details-marker {{ display: none; }}
  details.test-row summary::before {{ content: "▶"; font-size: 0.7rem; color: #94a3b8; }}
  details[open].test-row summary::before {{ content: "▼"; }}

  .test-detail {{ padding: 12px 16px; font-size: 0.85rem; display: flex; flex-direction: column; gap: 8px; }}
  .section-label {{ font-weight: 600; color: #475569; font-size: 0.75rem;
                    text-transform: uppercase; letter-spacing: .05em; margin-top: 6px; }}
  .tasks code {{ display: block; background: #f1f5f9; padding: 4px 8px;
                 border-radius: 4px; margin: 2px 0; font-size: 0.82rem; white-space: pre-wrap; }}
  .resp  {{ background: #f8fafc; padding: 6px 10px; border-radius: 4px;
            margin: 2px 0; font-size: 0.82rem; white-space: pre-wrap; border-left: 3px solid #cbd5e1; }}
  .reason {{ color: #374151; }}
  .manual {{ color: #7c3aed; font-weight: 500; }}
  .note   {{ color: #0369a1; }}
  .precond{{ color: #b45309; font-weight: 500; }}

  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 99px;
            font-size: 0.75rem; font-weight: 700; white-space: nowrap; }}
</style>
</head>
<body>
  <h1>Asmi Eval Report</h1>
  <div class="subtitle">Run at {run_time} &nbsp;·&nbsp; v0.10.37.2 regression suite &nbsp;·&nbsp; iMessage only</div>

  <div class="summary">
    <div class="stat"><div class="num">{total}</div><div class="lbl">Total</div></div>
    <div class="stat"><div class="num" style="color:#22c55e">{passed}</div><div class="lbl">Passed</div></div>
    <div class="stat"><div class="num" style="color:#ef4444">{failed}</div><div class="lbl">Failed</div></div>
    <div class="stat"><div class="num" style="color:#f59e0b">{other}</div><div class="lbl">Unclear</div></div>
    <div class="stat"><div class="num">{int(passed/total*100) if total else 0}%</div><div class="lbl">Pass rate</div></div>
  </div>

  {cat_rows}

</body>
</html>"""

    with open(output_path, "w") as f:
        f.write(html)

    print(f"\n  📄 Report saved to: {output_path}")
    return output_path
