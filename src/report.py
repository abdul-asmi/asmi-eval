# ─── HTML Report Generator ────────────────────────────────────────────────────

from collections import defaultdict
from datetime import datetime
import textwrap
import unicodedata
from zoneinfo import ZoneInfo


_VERDICT_COLOR = {"PASS": "#22c55e", "FAIL": "#ef4444", "UNCLEAR": "#f59e0b"}
_VERDICT_BG    = {"PASS": "#f0fdf4", "FAIL": "#fef2f2", "UNCLEAR": "#fffbeb"}

CATEGORIES = {
    "core test":       "Core Test",
    "onboarding":      "Onboarding",
    "capability":      "Capability",
    "sticky_message":  "Sticky Message",
    "call_dedup":      "Call Deduping",
    "call_summary":    "Post-Call Summary",
    "threep_nudge":    "3P Call Nudge",
    "location_memory": "Location Memory",
    "language_pref":   "Language Preference",
    "interactive":     "Interactive Conversations",
    "adhoc call":      "Ad-Hoc Call",
    "3P Calls":        "3P Calls",
    "PDF":             "PDF Generation",
}


def _target_label(asmi_target: str = "", asmi_handle: str = "") -> str:
    key = (asmi_target or "").strip().lower()
    handle = (asmi_handle or "").strip()
    if key == "prod":
        return "Prod"
    if key == "dev":
        return "Dev"
    if handle:
        return "Custom"
    return "Unknown"


def _pdf_safe_text(value: object) -> str:
    text = "" if value is None else str(value)
    text = (
        text.replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace("\t", "    ")
        .replace("•", "-")
        .replace("–", "-")
        .replace("—", "-")
        .replace("’", "'")
        .replace("“", '"')
        .replace("”", '"')
        .replace("→", "->")
        .replace("❌", "FAIL")
        .replace("✅", "PASS")
    )
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    return text


def _pdf_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _build_pdf_lines(results: list[dict], asmi_target: str = "", asmi_handle: str = "") -> list[str]:
    total = len(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    failed = sum(1 for r in results if r["verdict"] == "FAIL")
    other = total - passed - failed

    if not asmi_target and results:
        asmi_target = str(results[0].get("asmi_target") or "")
    if not asmi_handle and results:
        asmi_handle = str(results[0].get("asmi_handle") or "")

    target_label = _target_label(asmi_target, asmi_handle)
    target_detail = target_label + (f" ({asmi_handle})" if asmi_handle else "")
    run_time = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")

    by_cat = defaultdict(list)
    for result in results:
        by_cat[result["category"]].append(result)

    lines = [
        "ASMI EVAL REPORT",
        f"Run at: {run_time}",
        f"Target: {target_detail}",
        "",
        f"Total tests: {total}",
        f"Passed: {passed}",
        f"Failed: {failed}",
        f"Unclear: {other}",
        f"Pass rate: {int(passed / total * 100) if total else 0}%",
        "",
    ]

    for cat_key, cat_results in by_cat.items():
        cat_label = CATEGORIES.get(cat_key, cat_key)
        cat_pass = sum(1 for r in cat_results if r["verdict"] == "PASS")
        lines.extend([
            f"CATEGORY: {cat_label}",
            f"Score: {cat_pass}/{len(cat_results)}",
            "",
        ])

        for result in cat_results:
            lines.append(f"[{result.get('verdict', 'UNCLEAR')}] {result.get('id', '')} - {_pdf_safe_text(result.get('name', ''))}")
            if result.get("precondition"):
                lines.append(f"Precondition: {_pdf_safe_text(result['precondition'])}")
            lines.append(f"Judge: {_pdf_safe_text(result.get('reason', ''))}")

            tasks = result.get("tasks_sent", []) or []
            if tasks:
                lines.append("Tasks sent:")
                for task in tasks:
                    lines.append(f"  - {_pdf_safe_text(task)}")

            responses = result.get("responses", []) or []
            if responses:
                lines.append("Responses:")
                for idx, resp in enumerate(responses, start=1):
                    lines.append(f"  {idx}. {_pdf_safe_text(resp or 'no response')}")

            transcript = result.get("transcript", []) or []
            if transcript:
                lines.append("Transcript:")
                for turn in transcript:
                    lines.append(f"  Turn {turn.get('turn', '')}")
                    lines.append(f"    You: {_pdf_safe_text(turn.get('user', ''))}")
                    for idx, rsp in enumerate(turn.get("responses", []) or [], start=1):
                        lines.append(f"    Asmi {idx}: {_pdf_safe_text(rsp or 'no response')}")

            if result.get("manual_check"):
                lines.append(f"Manual check: {_pdf_safe_text(result['manual_check'])}")
            if result.get("note"):
                lines.append(f"Note: {_pdf_safe_text(result['note'])}")
            count_verdict = result.get("count_verdict") or {}
            if count_verdict:
                lines.append(
                    f"Count verdict: {_pdf_safe_text(count_verdict.get('verdict', ''))} - {_pdf_safe_text(count_verdict.get('reason', ''))}"
                )
            lines.append("")

        lines.append("")

    wrapped_lines: list[str] = []
    for line in lines:
        safe_line = _pdf_safe_text(line)
        if not safe_line:
            wrapped_lines.append("")
            continue
        wrapped_lines.extend(textwrap.wrap(safe_line, width=100, subsequent_indent="    ") or [""])
    return wrapped_lines


def generate_pdf(results: list[dict], output_path: str = "report.pdf", asmi_target: str = "", asmi_handle: str = ""):
    lines = _build_pdf_lines(results, asmi_target=asmi_target, asmi_handle=asmi_handle)

    page_width = 612
    page_height = 792
    margin = 48
    font_size = 10
    leading = 14
    max_lines = max(1, int((page_height - (margin * 2)) / leading))
    pages = [lines[i:i + max_lines] for i in range(0, len(lines), max_lines)] or [[]]

    def _page_stream(page_lines: list[str]) -> bytes:
        y_start = page_height - margin
        parts = ["BT", f"/F1 {font_size} Tf", f"{leading} TL", f"1 0 0 1 {margin} {y_start} Tm"]
        for idx, line in enumerate(page_lines):
            if idx > 0:
                parts.append("T*")
            parts.append(f"({_pdf_escape(line)}) Tj")
        parts.append("ET")
        return "\n".join(parts).encode("latin-1", "replace")

    objects: list[bytes] = []

    def _add_object(payload: bytes | str) -> int:
        raw = payload.encode("latin-1") if isinstance(payload, str) else payload
        objects.append(raw)
        return len(objects)

    font_id = _add_object("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids: list[int] = []
    content_ids: list[int] = []

    for page_lines in pages:
        stream = _page_stream(page_lines)
        content_id = _add_object(
            b"<< /Length " + str(len(stream)).encode("ascii") + b" >>\nstream\n" + stream + b"\nendstream"
        )
        content_ids.append(content_id)
        page_id = _add_object(
            f"<< /Type /Page /Parent 0 0 R /MediaBox [0 0 {page_width} {page_height}] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >>"
        )
        page_ids.append(page_id)

    kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
    pages_id = _add_object(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>")
    catalog_id = _add_object(f"<< /Type /Catalog /Pages {pages_id} 0 R >>")

    for page_id in page_ids:
        objects[page_id - 1] = objects[page_id - 1].replace(b"/Parent 0 0 R", f"/Parent {pages_id} 0 R".encode("ascii"))

    pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(pdf))
        pdf.extend(f"{idx} 0 obj\n".encode("ascii"))
        pdf.extend(obj)
        pdf.extend(b"\nendobj\n")

    xref_start = len(pdf)
    pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("ascii"))
    pdf.extend(b"0000000000 65535 f \n")
    for offset in offsets[1:]:
        pdf.extend(f"{offset:010d} 00000 n \n".encode("ascii"))
    pdf.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_start}\n%%EOF\n".encode("ascii")
    )

    with open(output_path, "wb") as f:
        f.write(pdf)

    print(f"  PDF report: {output_path}")
    return output_path


def generate(results: list[dict], output_path: str = "report.html", asmi_target: str = "", asmi_handle: str = ""):
    total  = len(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    failed = sum(1 for r in results if r["verdict"] == "FAIL")
    other  = total - passed - failed

    if not asmi_target and results:
        asmi_target = str(results[0].get("asmi_target") or "")
    if not asmi_handle and results:
        asmi_handle = str(results[0].get("asmi_handle") or "")
    target_label = _target_label(asmi_target, asmi_handle)
    target_color = "#991b1b" if target_label == "Prod" else "#1d4ed8" if target_label == "Dev" else "#475569"
    target_bg = "#fee2e2" if target_label == "Prod" else "#dbeafe" if target_label == "Dev" else "#f1f5f9"
    target_detail = f"{target_label}"
    if asmi_handle:
        target_detail += f" ({asmi_handle})"

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
            transcript = ""
            for turn in r.get("transcript", []) or []:
                turn_resps = "".join(
                    f'<div class="resp">Asmi {i+1}: {rsp or "<em>no response</em>"}</div>'
                    for i, rsp in enumerate(turn.get("responses", []))
                )
                transcript += f"""
                <div class="turn">
                  <div class="turn-head">Turn {turn.get('turn', '')}</div>
                  <div class="resp user">You: {turn.get('user', '')}</div>
                  {turn_resps}
                </div>
                """

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
                    <span class="badge" style="background:{target_bg};color:{target_color}">{target_label}</span>
                    <strong>[{r["id"]}]</strong> {r["name"]}
                </summary>
                <div class="test-detail">
                    {pre}
                    <div class="section-label">Tasks sent</div>
                    <div class="tasks">{tasks}</div>
                    <div class="section-label">Responses ({len(r.get("responses",[]))})</div>
                    {resps if resps else '<em>No responses captured</em>'}
                    {f'<div class="section-label">Transcript</div>{transcript}' if transcript else ''}
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

    run_time = datetime.now(ZoneInfo("America/New_York")).strftime("%Y-%m-%d %H:%M ET")
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
  .turn  {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 10px 12px; margin-top: 8px; }}
  .turn-head {{ font-weight: 700; color: #334155; margin-bottom: 6px; }}
  .resp.user {{ border-left-color: #3b82f6; background: #eef2ff; }}
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
  <div class="subtitle">Run at {run_time} &nbsp;·&nbsp; Target: {target_detail} &nbsp;·&nbsp; v0.10.37.2 regression suite &nbsp;·&nbsp; iMessage only</div>

  <div class="summary">
    <div class="stat"><div class="num">{total}</div><div class="lbl">Total</div></div>
    <div class="stat"><div class="num" style="font-size:1.35rem;color:{target_color}">{target_label}</div><div class="lbl">Target</div></div>
    <div class="stat"><div class="num" style="color:#22c55e">{passed}</div><div class="lbl">Passed</div></div>
    <div class="stat"><div class="num" style="color:#ef4444">{failed}</div><div class="lbl">Failed</div></div>
    <div class="stat"><div class="num" style="color:#f59e0b">{other}</div><div class="lbl">Unclear</div></div>
    <div class="stat"><div class="num">{int(passed/total*100) if total else 0}%</div><div class="lbl">Pass rate</div></div>
  </div>

  {cat_rows}

</body>
</html>"""

    html = "\n".join(line.rstrip() for line in html.splitlines()) + "\n"

    with open(output_path, "w") as f:
        f.write(html)

    print(f"\n  📄 Report saved to: {output_path}")
    return output_path
