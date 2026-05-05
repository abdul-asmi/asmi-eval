# ─── Test Runner ──────────────────────────────────────────────────────────────
# Phase 1: send all iMessages and collect responses (no Gemini calls)
# Phase 2: batch judge all results at the end (4s delay between calls)
#           — respects Gemini free tier ~15 RPM

import time
from datetime import datetime, timezone

from config import RESPONSE_TIMEOUT, BURST_WAIT, BURST_SEND_DELAY, SEQUENCE_DELAY, CMD_ONBOARD, CATEGORY_RUN_ORDER
from imessage import send_and_wait, send_burst, send_sequence, send_imessage, wait_for_responses
from judge import judge_with_context, judge_response_count

JUDGE_DELAY = 4  # seconds between Gemini calls (free tier safe)


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

    if test_type == "single":
        msg = tc["message"]
        result["tasks_sent"] = [msg]
        print(f"  → {msg[:80]}")
        result["responses"] = send_and_wait(msg, count=1, timeout=tc.get("wait", RESPONSE_TIMEOUT))

    elif test_type == "burst":
        msgs     = tc["messages"]
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = msgs
        result["responses"]  = send_burst(msgs, burst_delay=tc.get("burst_delay", BURST_SEND_DELAY),
                                          expected_responses=expected, timeout=tc.get("wait", BURST_WAIT))
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "burst_with_setup":
        setup = tc["setup_message"]
        msgs  = tc["messages"]
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = [setup] + msgs
        print(f"  → Setup: {setup}")
        send_imessage(setup)
        time.sleep(tc.get("setup_wait", 20))
        result["responses"] = send_burst(msgs, burst_delay=tc.get("burst_delay", BURST_SEND_DELAY),
                                         expected_responses=expected, timeout=tc.get("wait", BURST_WAIT))
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "sequence":
        msgs     = tc["messages"]
        expected = tc.get("expected_responses", len(msgs))
        result["tasks_sent"] = msgs
        responses = send_sequence(msgs, sequence_delay=tc.get("sequence_delay", SEQUENCE_DELAY),
                                  timeout_per=tc.get("wait", RESPONSE_TIMEOUT))
        result["responses"] = [r for r in responses if r]
        cv = judge_response_count(tc["name"], result["responses"], expected)
        result["count_verdict"] = cv

    elif test_type == "dedup":
        msg1  = tc["message"]
        msg2  = tc.get("dedup_message", msg1)
        delay = tc.get("dedup_delay", 2.0)
        expected = tc.get("expected_responses", 1)
        result["tasks_sent"] = [msg1, msg2]
        sent_at = datetime.now(timezone.utc)
        print(f"  → Sending msg 1: {msg1[:70]}")
        send_imessage(msg1)
        time.sleep(delay)
        print(f"  → Sending msg 2: {msg2[:70]}")
        send_imessage(msg2)
        result["responses"] = wait_for_responses(sent_at, count=expected + 1,
                                                 timeout=tc.get("wait", RESPONSE_TIMEOUT))
        # Dedup count check
        actual = len([r for r in result["responses"] if r])
        if actual > expected:
            result["count_verdict"] = {"verdict": "FAIL",
                                       "reason": f"Dedup failed — got {actual} responses, expected {expected}."}
        elif actual == 0:
            result["count_verdict"] = {"verdict": "FAIL", "reason": "No response received."}
        else:
            result["count_verdict"] = {"verdict": "PASS", "reason": f"Got {actual}/{expected} response(s)."}

    result["finished_at"] = datetime.now(timezone.utc).isoformat()
    resp_count = len([r for r in result["responses"] if r])
    print(f"  ✓ Collected {resp_count} response(s)")
    return result


# ── Phase 2: batch judge ───────────────────────────────────────────────────────

def batch_judge(results: list[dict], all_responses: list[str]) -> list[dict]:
    """
    Run Gemini judge on all collected results.
    Fires ONE Gemini call per test with a delay between each.
    """
    from test_cases import TEST_CASES
    tc_map = {t["id"]: t for t in TEST_CASES}

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


def run_all(test_cases: list[dict], filter_category: str = None, filter_id: str = None) -> list[dict]:
    to_run = test_cases
    if filter_category:
        to_run = [t for t in to_run if t["category"] == filter_category]
    if filter_id:
        to_run = [t for t in to_run if t["id"] == filter_id]

    # Apply priority ordering only for full runs (no category/id filter)
    running_all = not filter_category and not filter_id
    if running_all:
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
        cat = tc["category"]

        # Before the first onboarding test in a full run, reset Asmi to fresh state
        if running_all and cat == "onboarding" and last_category != "onboarding":
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

        r = collect(tc)
        results.append(r)
        time.sleep(5)  # brief pause between tests

    # Phase 2 — batch judge all at once
    pool    = _all_responses(results)
    results = batch_judge(results, all_responses=pool)

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
