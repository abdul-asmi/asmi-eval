import argparse
import os
import sys
import json
import time
import urllib.request
import urllib.parse
import ssl
from datetime import datetime, timezone

# Add 'src' directory to path
src_dir = os.path.dirname(os.path.abspath(__file__))
if src_dir not in sys.path:
    sys.path.insert(0, src_dir)

# Now import our modules
from config import REPORTS_DIR, RAILWAY_URL, LOCAL_UI_URL, DAEMON_TOKEN, DAEMON_OWNER_USER_ID, ASMI_HANDLE
from elevenlabs_phone import wait_for_call_transcript
from judge import judge_with_context
from runner import _save_and_send_call_recording, _all_responses
from report import generate, generate_pdf
from test_case_store import load_test_cases
from imessage import wait_for_responses, catch_up_manual_messages

# Disable SSL verification for local testing if needed
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

def main():
    parser = argparse.ArgumentParser(description="Detached Background Call Analyzer")
    parser.add_argument("--results-file", help="Path to the results_*.json file to update")
    parser.add_argument("--test-id", required=True, help="Test case ID to update")
    parser.add_argument("--call-started-after", required=True, help="ISO string of when the call started")
    parser.add_argument("--run-id", help="The Supabase Run ID")
    parser.add_argument("--asmi-target", default="", help="Target environment (dev/prod)")
    parser.add_argument("--asmi-handle", default="", help="Target Asmi iMessage handle")
    parser.add_argument("--preferred-convo-id", default="", help="Preferred ElevenLabs conversation ID")
    
    args = parser.parse_args()
    
    # 1. Setup logging
    log_name = f"bg_analyzer_{args.test_id}.log"
    log_path = os.path.join(REPORTS_DIR, log_name)
    
    # Redirect stdout and stderr to the log file so the detached process records its output safely
    log_file = open(log_path, "a", encoding="utf-8", buffering=1)
    sys.stdout = log_file
    sys.stderr = log_file
    
    print(f"\n==================================================")
    print(f"Background Analyzer started at {datetime.now(timezone.utc).isoformat()}")
    print(f"Arguments: {vars(args)}")
    
    # Pause briefly to allow the parent runner to write the results file and exit
    print("Waiting 3s for parent run_eval.py process to exit and write results...")
    time.sleep(3)
    
    # Find results file if not explicitly passed
    results_file = args.results_file
    if not results_file:
        latest_path = os.path.join(REPORTS_DIR, ".latest_results_path")
        if os.path.exists(latest_path):
            try:
                with open(latest_path, "r", encoding="utf-8") as f:
                    results_file = f.read().strip()
                print(f"Auto-detected results file from .latest_results_path: {results_file}")
            except Exception as e:
                print(f"Error reading .latest_results_path: {e}")
        
    if not results_file or not os.path.exists(results_file):
        # Fall back to searching reports directory for the latest results_*.json
        print("Results file not found/specified. Searching reports directory for latest results_*.json...")
        files = [os.path.join(REPORTS_DIR, f) for f in os.listdir(REPORTS_DIR) if f.startswith("results_") and f.endswith(".json")]
        if files:
            results_file = max(files, key=os.path.getmtime)
            print(f"Found latest results file in reports/: {results_file}")
        else:
            print("Fatal: Could not determine results file path!")
            log_file.close()
            sys.exit(1)
    
    # Parse call_started_after
    try:
        call_started_after = datetime.fromisoformat(args.call_started_after)
    except Exception as e:
        print(f"Error parsing call_started_after: {e}")
        call_started_after = datetime.now(timezone.utc)
        
    # Set ASMI_HANDLE env var so imessage.py polls the right number
    asmi_handle = (args.asmi_handle or "").strip()
    if asmi_handle:
        os.environ["ASMI_HANDLE"] = asmi_handle

    # 2. Wait for Call Transcript (ElevenLabs polling)
    print(f"Polling ElevenLabs for transcript (started after: {call_started_after})...")
    call_transcript_result = None
    try:
        # Timeout at 240 seconds (4 min cap), poll every 5 seconds
        call_transcript_result = wait_for_call_transcript(
            call_started_after=call_started_after,
            preferred_conversation_id=args.preferred_convo_id or None,
            timeout=240,
            poll_interval=5,
        )
    except Exception as e:
        print(f"Error wait_for_call_transcript: {e}")
        
    convo_id = ""
    if call_transcript_result:
        convo_id = call_transcript_result.get("conversation_id", "")
        print(f"Captured ElevenLabs conversation ID: {convo_id}")
    else:
        print("No transcript or conversation ID captured.")

    # 3. Retroactively capture post-call iMessage responses
    # Because the foreground runner only waited 8 seconds, it only got Asmi's
    # first "I've queued the call" acknowledgment. Now we wait for the actual
    # call-completion follow-up messages (e.g. "I connected with the gym and
    # your membership has been cancelled"). We poll chat.db starting from when
    # the call trigger message was sent (call_started_after) so no messages
    # are missed, even if they arrived late.
    print(f"Retroactively polling chat.db for post-call iMessage responses (handle={asmi_handle})...")
    late_imsg_responses = []
    try:
        late_raw = wait_for_responses(
            sent_at=call_started_after,
            count=4,                    # expect up to 4 follow-up messages
            timeout=120,               # wait up to 2 min for post-call messages
            handle=asmi_handle or None,
            max_responses=10,
            drain_all=True,
            return_raw=True,
            silence_after=25.0,        # 25s of silence = call is truly done
        )
        seen_keys_late = set()
        late_imsg_responses = [
            m for m in (late_raw or [])
            if not m.get("is_from_me")   # only Asmi's replies
        ]
        # Also do a catch-up sweep
        more = catch_up_manual_messages(call_started_after, seen_keys_late, handle=asmi_handle or None)
        late_imsg_responses.extend([m for m in more if not m.get("is_from_me")])
        print(f"Captured {len(late_imsg_responses)} post-call iMessage response(s) from chat.db")
    except Exception as e:
        print(f"Error capturing post-call iMessages: {e}")
        
    # 4. Media Handling: Audio download, Slack, iMessage
    recording_chat_state = {"status": "skipped", "sent": False, "error": "no call transcript/conversation id"}
    if convo_id:
        try:
            # We fetch/load the test cases to get the test name
            test_cases = load_test_cases()
            tc = next((t for t in test_cases if t["id"] == args.test_id), {})
            test_name = tc.get("name") or args.test_id
            
            print(f"Executing _save_and_send_call_recording for convo_id: {convo_id}")
            recording_chat_state = _save_and_send_call_recording(
                conversation_id=convo_id,
                test_id=args.test_id,
                test_name=test_name,
                call_transcript_result=call_transcript_result,
            )
            print(f"Media state: {recording_chat_state}")
        except Exception as e:
            print(f"Error saving and sending call recording: {e}")
            recording_chat_state = {"status": "error", "sent": False, "error": str(e)}

    # 5. Load results JSON file
    results = []
    if os.path.exists(results_file):
        try:
            with open(results_file, "r", encoding="utf-8") as f:
                results = json.load(f)
        except Exception as e:
            print(f"Error loading results file: {e}")
            
    # Find the result entry for our test case
    entry_idx = -1
    for idx, r in enumerate(results):
        if r.get("id") == args.test_id:
            entry_idx = idx
            break
            
    if entry_idx == -1:
        print(f"Error: test case {args.test_id} not found in results file!")
        results.append({"id": args.test_id})
        entry_idx = len(results) - 1

    r = results[entry_idx]
    
    # 6. Update call transcript fields
    if call_transcript_result:
        r["call_transcript"] = call_transcript_result.get("transcript_text", "")
        r["call_transcript_raw"] = call_transcript_result.get("transcript", [])
        r["call_conversation_id"] = call_transcript_result.get("conversation_id", "")
        r["call_duration_secs"] = call_transcript_result.get("duration_secs")
    else:
        r["call_transcript"] = "NO ELEVENLABS CALL TRANSCRIPT WAS CAPTURED."
        r["call_transcript_raw"] = []
        r["call_conversation_id"] = args.preferred_convo_id or ""
        r["call_duration_secs"] = None
        
    r["call_recording_chat"] = recording_chat_state
    
    # 7. Merge late iMessage responses into the result entry
    # Deduplicate against already-recorded responses
    existing_responses = set(r.get("responses") or [])
    newly_added = []
    for m in late_imsg_responses:
        text = (m.get("text") or "").strip()
        if text and text not in existing_responses:
            existing_responses.add(text)
            newly_added.append(text)
    if newly_added:
        r["responses"] = list(r.get("responses") or []) + newly_added
        print(f"Merged {len(newly_added)} new post-call iMessage response(s) into results")
    else:
        print("No new iMessage responses to merge (already captured or none received)")
    
    # 8. LLM Judging — now with both ElevenLabs transcript AND post-call iMessages
    test_cases = load_test_cases()
    tc = next((t for t in test_cases if t["id"] == args.test_id), {})
    criteria = tc.get("pass_criteria", "")
    
    if not criteria:
        r["verdict"] = "UNCLEAR"
        r["reason"] = "No pass_criteria in test_cases.py"
        print("Skipped judging (no criteria)")
    else:
        # Build all responses pool from the loaded results file
        pool = _all_responses(results)
        
        print(f"Invoking Gemini LLM judge with transcript + {len(r.get('responses', []))} iMessage responses...")
        try:
            llm = judge_with_context(
                test_name=r.get("name", args.test_id),
                category=r.get("category", ""),
                tasks=r.get("tasks_sent", []),
                captured=r.get("responses", []),
                all_responses=pool,
                pass_criteria=criteria,
                call_transcript=r.get("call_transcript"),
                elevenlabs_analysis=call_transcript_result.get("analysis") if call_transcript_result else None
            )
            r["verdict"] = llm["verdict"]
            r["reason"] = llm["reason"]
            if llm.get("matched_responses"):
                r["matched_responses"] = llm["matched_responses"]
            print(f"Gemini Verdict: {r['verdict']} - {r['reason']}")
        except Exception as e:
            print(f"LLM Judging error: {e}")
            r["verdict"] = "UNCLEAR"
            r["reason"] = f"Judging error: {e}"
            
    r["finished_at"] = datetime.now(timezone.utc).isoformat()
    
    # 9. Save updated results to the results_*.json file
    try:
        with open(results_file, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, default=str)
        print(f"Successfully saved updated results to {results_file}")
    except Exception as e:
        print(f"Error saving results: {e}")
        
    # 10. Regenerate HTML and PDF reports
    results_basename = os.path.basename(results_file)
    # results_20260522_114530.json -> 20260522_114530
    ts_part = results_basename.replace("results_", "").replace(".json", "")
    report_html_path = os.path.join(REPORTS_DIR, f"report_{ts_part}.html")
    report_pdf_path = os.path.join(REPORTS_DIR, f"report_{ts_part}.pdf")
    
    try:
        print(f"Regenerating HTML report: {report_html_path}")
        generate(results, output_path=report_html_path, asmi_target=args.asmi_target, asmi_handle=args.asmi_handle)
        
        print(f"Regenerating PDF report: {report_pdf_path}")
        generate_pdf(results, output_path=report_pdf_path, asmi_target=args.asmi_target, asmi_handle=args.asmi_handle)
    except Exception as e:
        print(f"Error regenerating reports: {e}")
        
    # 11. POST the final completed status and updated results to Render / local UI
    report_html_content = ""
    if os.path.exists(report_html_path):
        try:
            with open(report_html_path, "r", encoding="utf-8") as f:
                report_html_content = f.read()
        except Exception as e:
            print(f"Error reading report HTML content: {e}")

    verdict_sym = {"PASS": "✅", "FAIL": "❌", "UNCLEAR": "⚠️", "PENDING": "⏳"}.get(r.get("verdict", ""), "")
    output_str = (
        f"📞 Background call analysis complete for [{args.test_id}]\n"
        f"{verdict_sym} Verdict: {r.get('verdict', 'UNCLEAR')}\n"
        f"Reason: {r.get('reason', '')}\n"
        f"Call transcript: {len(r.get('call_transcript', ''))} chars\n"
        f"iMessage responses: {len(r.get('responses', []))}\n"
        f"Completed at: {r.get('finished_at', '')}"
    )

    for url, headers_extra in [
        (RAILWAY_URL, {"X-Daemon-Token": DAEMON_TOKEN, "X-Owner-User-Id": DAEMON_OWNER_USER_ID}),
        (LOCAL_UI_URL, {}),
    ]:
        if not url:
            continue
        try:
            print(f"POSTing updated output to: {url}/api/output")
            body_dict = {
                "run_id": args.run_id or os.environ.get("ASMI_RUN_ID") or "",
                "output": output_str,
                "status": "done",
                "results": results,
                "report_html": report_html_content,
                "asmi_target": args.asmi_target,
                "asmi_handle": args.asmi_handle,
            }
            body = json.dumps(body_dict, default=str).encode()
            req = urllib.request.Request(f"{url}/api/output", data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            for k, v in headers_extra.items():
                if v:
                    req.add_header(k, v)
            urllib.request.urlopen(req, timeout=20, context=_SSL_CTX)
            print(f"Successfully POSTed to {url}")
        except Exception as e:
            print(f"Error POSTing to {url}: {e}")
            
    print(f"Background Analyzer completed at {datetime.now(timezone.utc).isoformat()}")
    print(f"==================================================")
    log_file.close()

if __name__ == "__main__":
    main()
