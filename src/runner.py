import time
from datetime import datetime, timezone
import os
import json

from config import RESPONSE_TIMEOUT, BURST_WAIT, BURST_SEND_DELAY, SEQUENCE_DELAY, SILENCE_AFTER, CMD_ONBOARD, CATEGORY_RUN_ORDER, JUDGE_DELAY
from imessage import send_imessage, wait_for_responses
from judge import judge_status, judge_with_context, judge_response_count


def _stop_requested() -> bool:
    path = os.environ.get("ASMI_STOP_FILE", "").strip()
    return bool(path and os.path.exists(path))


def _sleep_interruptible(seconds: float) -> bool:
    """Sleep in short chunks. Returns False if a stop was requested."""
    deadline = time.time() + max(0.0, float(seconds or 0))
    while time.time() < deadline:
        if _stop_requested():
            print("\n  ⏹ Stop requested — not sending more messages")
            return False
        time.sleep(min(0.5, deadline - time.time()))
    return not _stop_requested()


def _skip_ids() -> set[str]:
    path = os.environ.get("ASMI_SKIP_FILE", "").strip()
    if not path or not os.path.exists(path):
        return set()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return {str(x).strip() for x in data if str(x).strip()}
    except Exception:
        return set()


def _interactive_auto_continue(tc: dict) -> bool:
    env = os.environ.get("ASMI_INTERACTIVE_AUTO_CONTINUE")
    if env is not None:
        return env.strip().lower() not in {"0", "false", "no", "off"}
    return bool(tc.get("auto_continue", True))


def _split_lines(val) -> list[str]:
    if val is None:
        return []
    if isinstance(val, list):
        out = []
        for v in val:
            out.extend(_split_lines(v))
        return out
    if isinstance(val, str):
        s = val.replace("\\n", "\n")
        return [line.strip() for part in s.split("|") for line in part.splitlines() if line.strip()]
    return [str(val).strip()] if str(val).strip() else []

def _messages_to_send(tc: dict) -> list[str]:
    """
    Best-effort reconstruction of the exact user messages a test will send.
    Used for runner-side setup decisions (e.g., avoid duplicate cmd_onboard).
    """
    tc = tc or {}
    t = str(tc.get("type") or "").strip()

    if t == "single":
        return _split_lines(tc.get("start_message") or tc.get("message"))
    if t in {"burst", "sequence"}:
        return _split_lines(tc.get("messages"))
    if t == "burst_with_setup":
        setup = _split_lines(tc.get("setup_message"))
        msgs = _split_lines(tc.get("messages"))
        return setup + msgs
    if t == "dedup":
        msg1 = _split_lines(tc.get("message"))
        msg2 = _split_lines(tc.get("dedup_message") or tc.get("message"))
        return msg1 + msg2
    if t == "interactive":
        start = _split_lines(tc.get("start_message") or tc.get("message"))
        followups = _split_lines(tc.get("followups") or tc.get("messages") or tc.get("replies"))
        return start + followups

    return _split_lines(tc.get("start_message") or tc.get("message") or tc.get("messages"))

def _group_raw_responses_by_turn(sent_turns: list[dict], raw_msgs: list[dict]) -> list[dict]:
    """
    Assign each raw assistant message to the most recent user turn whose
    started_at is <= message timestamp. Returns turns with responses filled.
    """
    turns = []
    for t in sent_turns:
        turns.append({
            "turn": t["turn"],
            "user": t["user"],
            "responses": [],
            "started_at": t["started_at"],
            "finished_at": t.get("finished_at") or t["started_at"],
        })

    # Ensure chronological order for assignment.
    raw_msgs = sorted(raw_msgs or [], key=lambda m: (m.get("timestamp") or ""))
    for m in raw_msgs:
        ts = m.get("timestamp")
        text = (m.get("text") or "").strip()
        if not ts or not text:
            continue
        # Find the latest turn whose started_at <= ts.
        idx = None
        for i in range(len(turns) - 1, -1, -1):
            if turns[i]["started_at"] <= ts:
                idx = i
                break
        if idx is None:
            continue
        turns[idx]["responses"].append(text)
        # Track last response time for display sorting.
        turns[idx]["finished_at"] = max(turns[idx]["finished_at"], ts)
    return turns


def reconstruct_transcript(session_start: datetime, raw_msgs: list[dict], default_user: str = None) -> list[dict]:
    """
    Reconstruct the transcript turns chronologically.
    Every user message (is_from_me = True) starts a new turn.
    Assistant replies (is_from_me = False) are grouped under the most recent user turn.
    If there are no user messages in raw_msgs but default_user is provided,
    we seed a single turn with default_user.
    """
    turns = []
    # Ensure raw_msgs are sorted chronologically
    sorted_msgs = sorted(raw_msgs or [], key=lambda m: m.get("timestamp") or datetime.min)
    
    current_turn = None
    turn_counter = 1
    
    for m in sorted_msgs:
        text = (m.get("text") or "").strip()
        if not text:
            continue
        ts = m.get("timestamp")
        ts_iso = ts.isoformat() if isinstance(ts, datetime) else str(ts)
        
        if m.get("is_from_me"):
            # It's a user message (automated or manual)
            current_turn = {
                "turn": turn_counter,
                "user": text,
                "responses": [],
                "started_at": ts_iso,
                "finished_at": ts_iso,
            }
            turns.append(current_turn)
            turn_counter += 1
        else:
            # It's an assistant response
            if current_turn is None:
                # Assistant replied before any user message was recorded in raw_msgs.
                # If default_user is provided, we can seed the first turn with it.
                user_text = default_user or "<System / Context>"
                start_time_iso = session_start.isoformat() if isinstance(session_start, datetime) else str(session_start)
                current_turn = {
                    "turn": turn_counter,
                    "user": user_text,
                    "responses": [],
                    "started_at": start_time_iso,
                    "finished_at": ts_iso,
                }
                turns.append(current_turn)
                turn_counter += 1
            current_turn["responses"].append(text)
            current_turn["finished_at"] = max(current_turn["finished_at"], ts_iso)
            
    if not turns and default_user:
        start_time_iso = session_start.isoformat() if isinstance(session_start, datetime) else str(session_start)
        turns.append({
            "turn": 1,
            "user": default_user,
            "responses": [],
            "started_at": start_time_iso,
            "finished_at": start_time_iso,
        })
        
    return turns


# ── Phase 1: collect responses ─────────────────────────────────────────────────

def collect(tc: dict) -> dict:
    """
    Send iMessages for one test case and collect responses.
    Does NOT call Gemini — just records raw responses.
    """
    test_id   = tc["id"]
    test_type = tc["type"]

    print(f"\n  [{test_id}] {tc['name']}")
    if tc.get("precondition"):
        print(f"  ⚠ Precondition: {tc['precondition']}")

    result = {
        "id":           test_id,
        "name":         tc["name"],
        "category":     tc["category"],
        "type":         test_type,
        "tasks_sent":   [],
        "responses":    [],
        "verdict":      "UNCLEAR",
        "reason":       "Pending judge",
        "count_verdict": None,
        "manual_check": tc.get("manual_check"),
        "note":         tc.get("note"),
        "started_at":   datetime.now(timezone.utc).isoformat(),
    }
    stopped_early = False

    if test_type == "single":
        if _stop_requested():
            stopped_early = True
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        msg = (tc.get("start_message") or tc.get("message") or "").strip()
        if not msg:
            raise KeyError(f"Test {test_id} (type=single) missing message/start_message")
        expected = int(tc.get("expected_responses") or 1)
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))

        result["tasks_sent"] = [msg]
        print(f"  → {msg[:80]}")
        sent_at = datetime.now(timezone.utc)
        ok = send_imessage(msg)
        if not ok:
            result["responses"] = []
            result["transcript"] = reconstruct_transcript(sent_at, [], default_user=msg)
        else:
            raw = wait_for_responses(
                sent_at,
                count=expected,
                timeout=wait,
                max_responses=max_responses,
                drain_all=True,
                return_raw=True,
                silence_after=silence_after,
            )
            transcript = reconstruct_transcript(sent_at, raw, default_user=msg)
            result["transcript"] = transcript
            result["tasks_sent"] = [t["user"] for t in transcript]
            result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
            cv = judge_response_count(tc["name"], result["responses"], expected)
            result["count_verdict"] = cv

    elif test_type == "burst":
        msgs     = _split_lines(tc["messages"])
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = msgs
        burst_delay = tc.get("burst_delay", BURST_SEND_DELAY)
        wait = tc.get("wait", BURST_WAIT)
        session_start = None
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            print(f"  → Sending [{i+1}/{len(msgs)}]: {msg[:70]}")
            send_imessage(msg)
            if i < len(msgs) - 1:
                if not _sleep_interruptible(burst_delay):
                    stopped_early = True
                    break
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        session_start = session_start or datetime.now(timezone.utc)
        raw = wait_for_responses(
            session_start,
            count=expected,
            timeout=wait,
            max_responses=max(10, expected + 6),
            drain_all=True,
            return_raw=True,
            silence_after=silence_after,
        )
        transcript = reconstruct_transcript(session_start, raw, default_user=msgs[0] if msgs else None)
        result["transcript"] = transcript
        result["tasks_sent"] = [t["user"] for t in transcript]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "burst_with_setup":
        setup = tc["setup_message"]
        msgs  = _split_lines(tc["messages"])
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = [setup] + msgs
        if _stop_requested():
            stopped_early = True
            result["tasks_sent"] = []
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        print(f"  → Setup: {setup}")
        setup_sent = datetime.now(timezone.utc)
        send_imessage(setup)
        if not _sleep_interruptible(tc.get("setup_wait", 20)):
            stopped_early = True
            result["transcript"] = reconstruct_transcript(setup_sent, [], default_user=setup)
            result["tasks_sent"] = [setup]
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — setup sent, not sending burst messages")
            return result
        burst_delay = tc.get("burst_delay", BURST_SEND_DELAY)
        wait = tc.get("wait", BURST_WAIT)
        session_start = setup_sent
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            print(f"  → Sending [{i+1}/{len(msgs)}]: {msg[:70]}")
            send_imessage(msg)
            if i < len(msgs) - 1:
                if not _sleep_interruptible(burst_delay):
                    stopped_early = True
                    break
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        raw = wait_for_responses(
            session_start,
            count=expected,
            timeout=wait,
            max_responses=max(10, expected + 6),
            drain_all=True,
            return_raw=True,
            silence_after=silence_after,
        )
        transcript = reconstruct_transcript(session_start, raw, default_user=setup)
        result["transcript"] = transcript
        result["tasks_sent"] = [t["user"] for t in transcript]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "sequence":
        msgs = _split_lines(tc["messages"])
        expected = int(tc.get("expected_responses") or len(msgs))
        result["tasks_sent"] = msgs
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))
        sequence_delay = tc.get("sequence_delay", SEQUENCE_DELAY)

        session_start = None
        seen = set()
        all_raw = []
        for i, msg in enumerate(msgs):
            if _stop_requested():
                stopped_early = True
                break
            print(f"\n  → Step [{i+1}/{len(msgs)}]: {msg[:70]}")
            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            ok = send_imessage(msg)
            if not ok:
                all_raw.append({
                    "text": msg,
                    "timestamp": sent_at,
                    "is_from_me": True
                })
            else:
                raw = wait_for_responses(
                    session_start,
                    count=1,
                    timeout=wait,
                    max_responses=max_responses,
                    drain_all=True,
                    return_raw=True,
                    silence_after=silence_after,
                )
                for m in raw or []:
                    key = (m.get("timestamp").isoformat() if m.get("timestamp") else "", m.get("text") or "", m.get("is_from_me"))
                    if key in seen:
                        continue
                    seen.add(key)
                    all_raw.append(m)
            if i < len(msgs) - 1:
                print(f"  (waiting {sequence_delay}s before next message…)")
                if not _sleep_interruptible(sequence_delay):
                    stopped_early = True
                    break

        session_start = session_start or datetime.now(timezone.utc)
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=msgs[0] if msgs else None)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in all_raw if (m.get("text") or "").strip() and not m.get("is_from_me")]
        cv = judge_response_count(tc["name"], [r for r in result["responses"] if r], expected)
        result["count_verdict"] = cv

    elif test_type == "dedup":
        msg1  = tc["message"]
        msg2  = tc.get("dedup_message", msg1)
        delay = tc.get("dedup_delay", 2.0)
        expected = tc.get("expected_responses", 1)
        result["tasks_sent"] = [msg1, msg2]
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        silence_after = float(tc.get("silence_after") or SILENCE_AFTER)
        max_responses = int(tc.get("max_responses") or max(10, expected + 6))
        session_start = datetime.now(timezone.utc)
        if _stop_requested():
            stopped_early = True
            result["reason"] = "Stopped before sending this test."
            result["finished_at"] = datetime.now(timezone.utc).isoformat()
            print("  ⏹ Stop requested — not sending this test")
            return result
        print(f"  → Sending msg 1: {msg1[:70]}")
        send_imessage(msg1)
        if _sleep_interruptible(delay):
            print(f"  → Sending msg 2: {msg2[:70]}")
            send_imessage(msg2)
        else:
            stopped_early = True

        raw = wait_for_responses(
            session_start,
            count=expected + 1,
            timeout=wait,
            max_responses=max_responses,
            drain_all=True,
            return_raw=True,
            silence_after=silence_after,
        )
        result["transcript"] = reconstruct_transcript(session_start, raw, default_user=msg1)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in (raw or []) if (m.get("text") or "").strip() and not m.get("is_from_me")]
        # Dedup count check
        actual = len([r for r in result["responses"] if r])
        if actual > expected:
            result["count_verdict"] = {"verdict": "FAIL",
                                       "reason": f"Dedup failed — got {actual} responses, expected {expected}."}
        elif actual == 0:
            result["count_verdict"] = {"verdict": "FAIL", "reason": "No response received."}
        else:
            result["count_verdict"] = {"verdict": "PASS", "reason": f"Got {actual}/{expected} response(s)."}

    elif test_type in {"interactive", "conversation"}:
        start_message = tc.get("start_message") or tc.get("message") or ""
        followups = _split_lines(tc.get("followups") or tc.get("messages") or tc.get("replies"))
        stop_when = _split_lines(tc.get("stop_when"))
        auto_continue = _interactive_auto_continue(tc)
        wait = tc.get("wait", RESPONSE_TIMEOUT)
        max_turns = int(tc.get("max_turns") or max(2, len(followups) + 1))
        max_responses = int(tc.get("max_responses") or 10)
        
        session_start = None
        seen_keys = set()
        all_raw = []

        def _hit_stop(text: str) -> bool:
            if not stop_when:
                return False
            hay = (text or "").lower()
            return any(term.lower() in hay for term in stop_when)

        print(f"  → Start: {start_message[:80]}")
        current_user = start_message
        for turn_idx in range(max_turns):
            if _stop_requested():
                stopped_early = True
                break
            if not current_user:
                break

            sent_at = datetime.now(timezone.utc)
            if session_start is None:
                session_start = sent_at
            
            # Send the automated user message
            send_imessage(current_user)
            
            poll = wait_for_responses(
                session_start,
                count=1,
                timeout=wait,
                max_responses=max_responses,
                drain_all=True,
                return_raw=True,
                silence_after=8.0,
            )
            for m in poll or []:
                key = (m.get("timestamp").isoformat() if m.get("timestamp") else "", m.get("text") or "", m.get("is_from_me"))
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                all_raw.append(m)

            # Get only the assistant responses for this step to check stop conditions
            step_responses = [m.get("text") for m in poll if m.get("text") and not m.get("is_from_me")]

            latest = " ".join(step_responses[-2:]) if step_responses else ""
            if _hit_stop(latest):
                result["verdict"] = "PASS"
                result["reason"] = "Interactive conversation reached the stop condition."
                break

            if not auto_continue:
                result["verdict"] = "UNCLEAR"
                result["reason"] = "Interactive run paused with auto-continue off."
                break

            if turn_idx >= len(followups):
                result["verdict"] = "UNCLEAR"
                result["reason"] = "Interactive follow-up script ended before the conversation naturally closed."
                break

            current_user = followups[turn_idx]

        session_start = session_start or datetime.now(timezone.utc)
        result["transcript"] = reconstruct_transcript(session_start, all_raw, default_user=start_message)
        result["tasks_sent"] = [t["user"] for t in result["transcript"]]
        result["responses"] = [m.get("text") for m in all_raw if (m.get("text") or "").strip() and not m.get("is_from_me")]
        result["auto_continue"] = auto_continue
        result["max_turns"] = max_turns
        result["max_responses"] = max_responses

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    if stopped_early:
        result["reason"] = "Stopped early; judging responses captured so far."
    resp_count = len([r for r in result["responses"] if r])
    print(f"  ✓ Collected {resp_count} response(s)")
    return result


# ── Phase 2: batch judge ───────────────────────────────────────────────────────

def batch_judge(results: list[dict], all_responses: list[str], test_cases: list[dict]) -> list[dict]:
    """
    Run Gemini judge on all collected results.
    Fires ONE Gemini call per test with a delay between each.
    """
    tc_map = {t.get("id"): t for t in (test_cases or []) if t.get("id")}

    print(f"\n{'─'*65}")
    print(f"  JUDGING {len(results)} test(s) — {JUDGE_DELAY}s between calls (free tier)")
    print(f"  Est. time: ~{len(results) * JUDGE_DELAY // 60}m {len(results) * JUDGE_DELAY % 60}s")
    print(f"{'─'*65}")

    for i, r in enumerate(results):
        tc       = tc_map.get(r["id"], {})
        criteria = tc.get("pass_criteria", "")

        print(f"\n  [{i+1}/{len(results)}] Judging [{r['id']}]…", end=" ", flush=True)

        if not criteria:
            r["verdict"] = "UNCLEAR"
            r["reason"]  = "No pass_criteria in test_cases.py"
            print("⚠ skipped (no criteria)")
            continue

        # If count already failed, skip LLM and mark FAIL
        if r.get("count_verdict") and r["count_verdict"]["verdict"] == "FAIL":
            r["verdict"] = "FAIL"
            r["reason"]  = r["count_verdict"]["reason"]
            print(f"❌ FAIL (count check)")
            continue

        llm = judge_with_context(
            test_name     = r["name"],
            category      = r["category"],
            tasks         = r.get("tasks_sent", []),
            captured      = r.get("responses", []),
            all_responses = all_responses,
            pass_criteria = criteria,
        )
        r["verdict"] = llm["verdict"]
        r["reason"]  = llm["reason"]
        if llm.get("matched_responses"):
            r["matched_responses"] = llm["matched_responses"]

        icon = {"PASS": "✅", "FAIL": "❌", "UNCLEAR": "⚠️"}.get(r["verdict"], "?")
        print(f"{icon} {r['verdict']}")

        status = judge_status()
        if not status["available"]:
            print(f"  Judge disabled for remaining tests: {status['reason']}")
            for rest in results[i + 1:]:
                if rest.get("verdict") in {"PASS", "FAIL"}:
                    continue
                rest["verdict"] = "UNCLEAR"
                rest["reason"] = status["reason"]
            break

        if i < len(results) - 1:
            time.sleep(JUDGE_DELAY)

    return results


# ── Collect all responses into a flat pool ─────────────────────────────────────

def _all_responses(results: list[dict]) -> list[str]:
    seen, pool = set(), []
    for r in results:
        for resp in r.get("responses", []):
            if resp and resp.strip() and resp not in seen:
                seen.add(resp)
                pool.append(resp.strip())
    return pool


# ── Main entry point ───────────────────────────────────────────────────────────

def _sort_by_priority(test_cases: list[dict]) -> list[dict]:
    """Sort tests by CATEGORY_RUN_ORDER, preserving original order within each category."""
    order = {cat: i for i, cat in enumerate(CATEGORY_RUN_ORDER)}
    return sorted(test_cases, key=lambda t: order.get(t["category"], len(CATEGORY_RUN_ORDER)))


def run_all(test_cases: list[dict], filter_category: str = None, filter_categories: list[str] = None, filter_id: str = None, filter_ids: list[str] = None) -> list[dict]:
    to_run = test_cases
    if filter_categories:
        to_run = [t for t in to_run if t["category"] in filter_categories]
    if filter_category:
        to_run = [t for t in to_run if t["category"] == filter_category]
    if filter_id:
        to_run = [t for t in to_run if t["id"] == filter_id]
    if filter_ids:
        to_run = [t for t in to_run if t["id"] in filter_ids]

    # Apply priority ordering only for full runs (no category/id filter)
    running_all = not filter_category and not filter_id and not filter_categories and not filter_ids
    if running_all:
        to_run = [t for t in to_run if t["category"] != "interactive"]
        to_run = _sort_by_priority(list(to_run))

    print(f"\n{'═'*65}")
    print(f"  ASMI EVAL — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    if running_all:
        print(f"  Priority order: {', '.join(CATEGORY_RUN_ORDER[:5])}…")
    print(f"  Phase 1: Sending {len(to_run)} test(s) via iMessage")
    print(f"  Phase 2: Batch judging at the end (Gemini free tier safe)")
    print(f"{'═'*65}")

    # Phase 1 — send all, collect responses
    results      = []
    last_category = None
    for tc in to_run:
        if tc["id"] in _skip_ids():
            print(f"\n  ⏭ Skipping [{tc['id']}] by request")
            continue
        if _stop_requested():
            print("\n  ⏹ Stop requested — judging captured results so far")
            break

        cat = tc["category"]

        # Before the first onboarding test in a full run, reset Asmi to fresh state
        if running_all and cat == "onboarding" and last_category != "onboarding":
            # Avoid double-sending cmd_onboard if the first onboarding test
            # already starts with it.
            first_msg = (_messages_to_send(tc)[:1] or [""])[0]
            if first_msg == CMD_ONBOARD:
                print(f"\n  ━━━ Skipping extra cmd_onboard (test already starts with it) ━━━")
            else:
                print(f"\n  ━━━ Sending cmd_onboard to reset Asmi state ━━━")
                send_imessage(CMD_ONBOARD)
                print(f"  Waiting 15s for Asmi to reset…")
                time.sleep(15)

        # Print a separator when switching categories
        if cat != last_category:
            print(f"\n{'─'*65}")
            print(f"  CATEGORY: {cat.upper()}")
            print(f"{'─'*65}")
            last_category = cat

        try:
            r = collect(tc)
        except Exception as e:
            print(f"  ❌ Error collecting responses for {tc.get('id')}: {e}")
            r = {
                "id":           tc.get("id"),
                "name":         tc.get("name", "Unknown"),
                "category":     tc.get("category", "Unknown"),
                "type":         tc.get("type", "single"),
                "tasks_sent":   [],
                "responses":    [],
                "verdict":      "FAIL",
                "reason":       f"Setup error: {e}",
                "count_verdict": {"verdict": "FAIL", "reason": str(e)},
                "manual_check": tc.get("manual_check"),
                "note":         tc.get("note"),
                "started_at":   datetime.now(timezone.utc).isoformat(),
                "finished_at":  datetime.now(timezone.utc).isoformat(),
            }
        results.append(r)
        if _stop_requested():
            print("\n  ⏹ Stop requested — judging captured results so far")
            break
        time.sleep(5)  # brief pause between tests

    # Phase 2 — batch judge all at once
    pool    = _all_responses(results)
    results = batch_judge(results, all_responses=pool, test_cases=test_cases)

    # Summary
    total   = len(results)
    passed  = sum(1 for r in results if r["verdict"] == "PASS")
    failed  = sum(1 for r in results if r["verdict"] == "FAIL")
    other   = total - passed - failed

    print(f"\n{'═'*65}")
    print(f"  DONE: {passed} passed / {failed} failed / {other} unclear — {total} total")
    print(f"  Pass rate: {int(passed/total*100) if total else 0}%")
    print(f"{'═'*65}")

    if failed:
        print("\n  ❌ Failures:")
        for r in results:
            if r["verdict"] == "FAIL":
                print(f"     [{r['id']}] {r['name']}")
                print(f"       → {r['reason']}")

    return results
