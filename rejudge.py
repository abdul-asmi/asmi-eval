#!/usr/bin/env python3
"""
rejudge.py — Re-run the Gemini judge on an existing results JSON.
No iMessages are sent.

Key behaviour: passes ALL responses from the entire run to Gemini so it can
find which response(s) actually answer each specific test question, rather than
blindly evaluating whatever was captured in that test's time window.

Usage:
    python rejudge.py results_20260504_1402.json
    python rejudge.py results_20260504_1402.json --category call_dedup
    python rejudge.py results_20260504_1402.json --id dedup_01
"""

import argparse
import json
import time
from datetime import datetime

from judge import judge_with_context, judge_response_count
from report import generate

JUDGE_DELAY = 4  # seconds between Gemini calls (free tier ~15 RPM)


def collect_all_responses(results: list[dict]) -> list[str]:
    """Flatten every response captured across the entire run into one list."""
    all_responses = []
    for r in results:
        for resp in r.get("responses", []):
            if resp and resp.strip():
                all_responses.append(resp.strip())
    # Deduplicate while preserving order
    seen = set()
    deduped = []
    for resp in all_responses:
        if resp not in seen:
            seen.add(resp)
            deduped.append(resp)
    return deduped


def rejudge(results: list[dict], all_responses: list[str]) -> list[dict]:
    updated = []

    # Load test cases for pass_criteria and expected_responses
    try:
        from test_cases import TEST_CASES
        tc_map = {t["id"]: t for t in TEST_CASES}
    except Exception:
        tc_map = {}

    for r in results:
        print(f"\n  [{r['id']}] {r['name']}")

        tc       = tc_map.get(r["id"], {})
        criteria = tc.get("pass_criteria", "")
        tasks    = r.get("tasks_sent", [])

        if not criteria:
            print(f"  ⚠ No pass_criteria found — skipping")
            r["verdict"] = "UNCLEAR"
            r["reason"]  = "No pass_criteria found in test_cases.py"
            updated.append(r)
            continue

        # Count check (uses originally captured responses)
        expected = tc.get("expected_responses")
        if expected and r.get("type") in ("burst", "burst_with_setup", "sequence"):
            count_v = judge_response_count(r["name"], r.get("responses", []), expected)
            r["count_verdict"] = count_v
            print(f"  Count: {count_v['verdict']} — {count_v['reason']}")

        # Rate limit: 4s between Gemini calls (free tier ~15 RPM)
        if updated:
            time.sleep(JUDGE_DELAY)

        # LLM judge — passes full response pool so it can find the right answer
        llm = judge_with_context(
            test_name     = r["name"],
            category      = r["category"],
            tasks         = tasks,
            captured      = r.get("responses", []),
            all_responses = all_responses,
            pass_criteria = criteria,
        )
        r["verdict"] = llm["verdict"]
        r["reason"]  = llm["reason"]
        if llm.get("matched_responses"):
            r["matched_responses"] = llm["matched_responses"]

        # Downgrade to FAIL if count check failed
        if r.get("count_verdict") and r["count_verdict"]["verdict"] == "FAIL":
            r["verdict"] = "FAIL"
            r["reason"]  = r["count_verdict"]["reason"] + " | " + llm.get("reason", "")

        icon = {"PASS": "✅", "FAIL": "❌", "UNCLEAR": "⚠️"}.get(r["verdict"], "?")
        print(f"  {icon} {r['verdict']} — {r['reason']}")
        updated.append(r)

    return updated


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("results_file", help="Path to results_*.json")
    parser.add_argument("--id",       help="Only re-judge this test ID")
    parser.add_argument("--category", help="Only re-judge this category")
    args = parser.parse_args()

    with open(args.results_file) as f:
        results = json.load(f)

    # Build the full response pool from the entire run
    all_responses = collect_all_responses(results)
    print(f"\n  Total unique responses across entire run: {len(all_responses)}")

    # Filter what to rejudge
    to_judge = results
    if args.id:
        to_judge = [r for r in results if r["id"] == args.id]
    if args.category:
        to_judge = [r for r in results if r["category"] == args.category]

    print(f"\n{'═'*65}")
    print(f"  RE-JUDGING {len(to_judge)} test(s) from {args.results_file}")
    print(f"  Gemini sees ALL {len(all_responses)} responses to find the right match")
    print(f"{'═'*65}")

    updated = rejudge(to_judge, all_responses)

    # Merge back if filtered
    if args.id or args.category:
        updated_ids = {r["id"] for r in updated}
        merged = [r for r in results if r["id"] not in updated_ids] + updated
        merged.sort(key=lambda r: r.get("id", ""))
    else:
        merged = updated

    # Summary
    total  = len(merged)
    passed = sum(1 for r in merged if r["verdict"] == "PASS")
    failed = sum(1 for r in merged if r["verdict"] == "FAIL")
    other  = total - passed - failed

    print(f"\n{'═'*65}")
    print(f"  RESULTS: {passed} passed / {failed} failed / {other} unclear")
    print(f"  Pass rate: {int(passed/total*100) if total else 0}%")
    print(f"{'═'*65}")

    if failed:
        print("\n  ❌ Failed:")
        for r in merged:
            if r["verdict"] == "FAIL":
                print(f"     [{r['id']}] {r['name']}")
                print(f"       → {r['reason']}")

    ts         = datetime.now().strftime("%Y%m%d_%H%M")
    json_out   = f"results_rejudged_{ts}.json"
    report_out = f"report_rejudged_{ts}.html"

    with open(json_out, "w") as f:
        json.dump(merged, f, indent=2, default=str)

    generate(merged, output_path=report_out)
    print(f"\n  💾 Results: {json_out}")
    print(f"  🌐 Report:  open {report_out}\n")


if __name__ == "__main__":
    main()
