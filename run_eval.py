#!/usr/bin/env python3
# ─── Asmi iMessage Eval — Entry Point ────────────────────────────────────────
#
# Usage:
#   python run_eval.py                          # run all tests
#   python run_eval.py --category sticky_message
#   python run_eval.py --category call_dedup
#   python run_eval.py --id sticky_03
#   python run_eval.py --list                   # list all test IDs and names
#
# Categories:
#   sticky_message | call_dedup | call_summary | language_pref
#   location_memory | onboarding | capability | threep_nudge
#
# Requirements:
#   pip install google-generativeai
#
# macOS setup:
#   Grant Terminal Full Disk Access:
#   System Settings → Privacy & Security → Full Disk Access → enable Terminal
# ─────────────────────────────────────────────────────────────────────────────

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

# Auto-sync test cases from GitHub before every run
subprocess.run(["git", "pull", "--quiet"], cwd=os.path.dirname(os.path.abspath(__file__)))

from test_cases import TEST_CASES
from runner import run_all
from report import generate


def main():
    parser = argparse.ArgumentParser(description="Asmi iMessage Eval Runner")
    parser.add_argument("--category", help="Only run tests in this category")
    parser.add_argument("--categories", help="Only run tests in these categories (comma-separated)")
    parser.add_argument("--id",       help="Only run the test with this ID")
    parser.add_argument("--ids",      help="Only run these test IDs (comma-separated)")
    parser.add_argument("--list",     action="store_true", help="List all tests and exit")
    parser.add_argument("--no-report",action="store_true", help="Skip HTML report generation")
    args = parser.parse_args()

    # ── list mode ─────────────────────────────────────────────────────────────
    if args.list:
        print(f"\n{'ID':<15} {'CATEGORY':<20} NAME")
        print("─" * 70)
        for tc in TEST_CASES:
            pre = " [needs fresh acct]" if tc.get("precondition") else ""
            print(f"{tc['id']:<15} {tc['category']:<20} {tc['name']}{pre}")
        print(f"\nTotal: {len(TEST_CASES)} tests\n")
        return

    # ── run ───────────────────────────────────────────────────────────────────
    categories = None
    if args.categories:
        categories = [c.strip() for c in args.categories.split(',') if c.strip()]
    ids = None
    if args.ids:
        ids = [i.strip() for i in args.ids.split(',') if i.strip()]
    results = run_all(
        TEST_CASES,
        filter_category=args.category,
        filter_categories=categories,
        filter_id=args.id,
        filter_ids=ids,
    )

    # ── summary ───────────────────────────────────────────────────────────────
    total  = len(results)
    passed = sum(1 for r in results if r["verdict"] == "PASS")
    failed = sum(1 for r in results if r["verdict"] == "FAIL")
    other  = total - passed - failed

    print(f"\n{'═'*70}")
    print(f"  RESULTS: {passed} passed / {failed} failed / {other} unclear — {total} total")
    print(f"  Pass rate: {int(passed/total*100) if total else 0}%")
    print(f"{'═'*70}")

    if failed:
        print("\n  ❌ Failed tests:")
        for r in results:
            if r["verdict"] == "FAIL":
                print(f"     [{r['id']}] {r['name']}")
                print(f"       → {r['reason']}")

    # ── save JSON results ──────────────────────────────────────────────────────
    import sys as _sys
    _reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")
    os.makedirs(_reports_dir, exist_ok=True)

    ts          = datetime.now(ZoneInfo("America/New_York")).strftime("%Y%m%d_%H%M%S")
    json_path   = os.path.join(_reports_dir, f"results_{ts}.json")
    report_path = os.path.join(_reports_dir, f"report_{ts}.html")

    with open(json_path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Raw results: {json_path}")

    # ── HTML report ────────────────────────────────────────────────────────────
    if not args.no_report:
        generate(results, output_path=report_path)
        print(f"  Open report: open {report_path}\n")


if __name__ == "__main__":
    main()
