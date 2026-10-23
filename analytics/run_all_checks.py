"""
run_all_checks.py
-----------------
Stage 8: one command that runs every Person 3 check and prints a pass/fail table.
Run from the project root:
    python -m analytics.run_all_checks
"""

import subprocess
import sys

STEPS = [
    ("Bundle decomposition self-tests", ["-m", "analytics.bundle_decomposition", "--selftest-only"]),
    ("VIS self-tests", ["-m", "analytics.vis_score", "--selftest-only"]),
    ("Insights self-tests (history + response)", ["-m", "analytics.insights", "--selftest-only"]),
    ("Watch rule self-tests", ["-m", "analytics.watch_rules", "--selftest-only"]),
    ("Report checker self-tests", ["-m", "analytics.report_checker", "--selftest-only"]),
    ("Card checker self-tests", ["-m", "analytics.card_checker", "--selftest-only"]),
    ("Rebuild all reports (includes VIS section)", ["-m", "analytics.report_builder"]),
    ("Stored reports pass the checker", ["-m", "analytics.report_checker", "--stored-only"]),
    ("Stored cards pass the checker", ["-m", "analytics.card_checker", "--stored-only"]),
    ("Live analysis re-run on every change", ["-m", "analytics.live_analysis", "--force"]),
]
BAD_MARKERS = ("[WRONG]", "[FAILED]", "Traceback", "VIS failed", "analysis failed")


def main() -> int:
    results = []
    for name, args in STEPS:
        proc = subprocess.run([sys.executable, *args], capture_output=True, text=True)
        text = (proc.stdout or "") + (proc.stderr or "")
        bad = [m for m in BAD_MARKERS if m in text]
        ok = proc.returncode == 0 and not bad
        results.append((name, ok))
        print(f"[{'PASS' if ok else 'FAIL'}] {name}")
        if not ok:
            tail = "\n".join(text.strip().splitlines()[-12:])
            print("   ---- last lines ----\n" + "\n".join("   " + ln for ln in tail.splitlines()))
    passed = sum(1 for _, ok in results if ok)
    print(f"\n{passed}/{len(results)} checks passed.")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())