"""
report_checker.py
-----------------
Deterministic checker for sourced reports (no LLM). A report passes only if every
sentence passes analytics.fact_checks.check_sentence AND every origin of the cited
claims (for example "simulated") is declared in report["origins"].

Run from the project root:
    python -m analytics.report_checker                 # re-check stored reports + self-tests
    python -m analytics.report_checker --stored-only
    python -m analytics.report_checker --selftest-only
"""

import argparse
import sys

from analytics.claim_store import get_claims_by_ids
from analytics.fact_checks import (
    build_citations,
    check_sentence,
    claim_origin,
    clean_claim_ids,
)
from analytics.bundle_decomposition import check_rupee_rule
from analytics.report_store import ensure_tables, list_reports, set_checker_result


def check_report(report: dict) -> dict:
    """{"passed": bool, "issues": [{sentence_id, code, message}], "sentences_checked": int}"""
    issues = []
    sentences = report.get("sentences") if isinstance(report, dict) else None
    if not isinstance(sentences, list) or not sentences:
        return {
            "passed": False, "sentences_checked": 0,
            "issues": [{"sentence_id": None, "code": "empty_report",
                        "message": "A report must contain at least one sentence."}],
        }

    seen_origins = set()
    for idx, sentence in enumerate(sentences, start=1):
        sid = sentence.get("id") if isinstance(sentence, dict) else None
        sid = sid or f"#{idx}"
        for issue in check_sentence(sentence):
            issues.append({"sentence_id": sid, **issue})
        for issue in check_rupee_rule(sentence):
            issues.append({"sentence_id": sid, **issue})
        if isinstance(sentence, dict):
            ids = clean_claim_ids(sentence.get("claim_ids"))
            for claim in get_claims_by_ids(ids).values():
                seen_origins.add(claim_origin(claim))

    declared = set(report.get("origins") or [])
    missing = sorted(seen_origins - declared)
    if missing:
        issues.append({
            "sentence_id": None, "code": "origin_label_missing",
            "message": f"Cited claims have origin {missing} but the report does not declare it.",
        })
    return {"passed": not issues, "issues": issues, "sentences_checked": len(sentences)}


# ── Re-check stored reports ──────────────────────────────────────────────────

def recheck_stored() -> bool:
    ensure_tables()
    rows = list_reports()
    if not rows:
        print("No stored reports yet. Run: python -m analytics.report_builder")
        return True
    all_ok = True
    for row in rows:
        result = check_report(row["report"])
        set_checker_result(row["report_id"], result["passed"])
        status = "PASSED" if result["passed"] else "REJECTED"
        print(f"[{status}] {row['report_id']}: {result['sentences_checked']} sentences")
        for i in result["issues"]:
            print(f"    {i['sentence_id']} {i['code']}: {i['message']}")
        all_ok = all_ok and result["passed"]
    return all_ok


# ── Self-tests: the checker must reject bad input ───────────────────────────

A_OLD = "SIM-isp-speedfiber-fup_cap_gb-T0"
A_NEW = "SIM-isp-speedfiber-fup_cap_gb-T1"
P_OLD = "SIM-isp-speedfiber-price_inr_per_month-T0"
P_NEW = "SIM-isp-speedfiber-price_inr_per_month-T1"
DECOY = "SIM-isp-bharatlink-speed_mbps-UNVERIFIED"

GOOD_TEXT = "SpeedFiber changed the FUP data cap of Fiber Ultra from 3300 GB to 1500 GB."


def _mk(text, ids, lane="evidence", persona_id=None) -> dict:
    s = {"id": "t", "section": "test", "lane": lane, "text": text,
         "claim_ids": list(ids), "citations": build_citations(ids)}
    if persona_id:
        s["persona_id"] = persona_id
    return s


def _build_tests() -> list[tuple]:
    both = [A_OLD, A_NEW]
    four = [A_OLD, A_NEW, P_OLD, P_NEW]

    tampered = _mk(GOOD_TEXT, both)
    tampered["citations"][0]["source_url"] = "http://evil.example/fake"

    uncited = _mk(GOOD_TEXT, both)
    uncited["citations"] = []

    no_lane = _mk(GOOD_TEXT, both)
    no_lane["lane"] = None

    ok_sentence = _mk(GOOD_TEXT, both)

    persona_text = "SpeedFiber has a FUP data cap of 1500 GB, below the 2000 GB the persona requires."

    # (name, kind, object, expected issue code or None for "must pass")
    return [
        ("valid evidence sentence passes", "sentence", ok_sentence, None),
        ("sentence with no claim ID is rejected", "sentence", _mk(GOOD_TEXT, []), "no_claim_id"),
        ("unknown claim ID is rejected", "sentence", _mk(GOOD_TEXT, ["C-DOES-NOT-EXIST"]), "unknown_claim"),
        ("fabricated number is rejected", "sentence",
         _mk("SpeedFiber changed the FUP data cap of Fiber Ultra from 3300 GB to 1200 GB.", both),
         "number_not_in_facts"),
        ("fabricated number glued to its unit is rejected", "sentence",
         _mk("SpeedFiber changed the FUP data cap of Fiber Ultra from 3300 GB to 1200GB.", both),
         "number_not_in_facts"),
        ("unverified claim is rejected", "sentence",
         _mk("BharatLink offers 999 Mbps.", [DECOY]), "unverified_claim"),
        ("derived number in the evidence lane is rejected", "sentence",
         _mk("Cost per GB rose to 0.5327 Rs/GB.", four, lane="evidence"), "number_not_in_facts"),
        ("same derived number in the model lane passes", "sentence",
         _mk("Cost per GB rose to 0.5327 Rs/GB.", four, lane="model"), None),
        ("truncated derived number (0.5) is rejected", "sentence",
         _mk("Cost per GB rose to 0.5 Rs/GB.", four, lane="model"), "number_not_in_facts"),
        ("integer truncation (1) is rejected", "sentence",
         _mk("Cost per GB rose to 1 Rs/GB.", four, lane="model"), "number_not_in_facts"),
        ("correct percentage change passes", "sentence",
         _mk("Cost per GB is up by 120.00%.", four, lane="model"), None),
        ("wrong percentage change is rejected", "sentence",
         _mk("Cost per GB is up by 125.00%.", four, lane="model"), "number_not_in_facts"),
        ("tampered citation is rejected", "sentence", tampered, "citation_mismatch"),
        ("missing citation is rejected", "sentence", uncited, "citation_missing"),
        ("missing lane is rejected", "sentence", no_lane, "bad_lane"),
        ("persona threshold without persona_id is rejected", "sentence",
         _mk(persona_text, [A_NEW], lane="model"), "number_not_in_facts"),
        ("persona threshold with persona_id passes", "sentence",
         _mk(persona_text, [A_NEW], lane="model", persona_id="heavy_streaming_household"), None),
        ("report that hides the simulated label is rejected", "report",
         {"sentences": [_mk(GOOD_TEXT, both)], "origins": []}, "origin_label_missing"),
        ("empty report is rejected", "report", {"sentences": []}, "empty_report"),
    ]


def run_selftests() -> bool:
    needed = [A_OLD, A_NEW, P_OLD, P_NEW, DECOY]
    if len(get_claims_by_ids(needed)) < len(needed):
        print("Self-tests need the seeded demo claims. Run: python -m analytics.seed_demo_claims")
        return False

    tests = _build_tests()
    good = 0
    for name, kind, obj, expect in tests:
        issues = check_sentence(obj) if kind == "sentence" else check_report(obj)["issues"]
        codes = sorted({i["code"] for i in issues})
        behaved = (not issues) if expect is None else (expect in codes)
        good += 1 if behaved else 0
        outcome = "passed" if not issues else "rejected " + ",".join(codes)
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")
    print(f"Self-tests: {good}/{len(tests)} behaved as expected.")
    return good == len(tests)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Report checker")
    parser.add_argument("--stored-only", action="store_true")
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()

    ok = True
    if not args.selftest_only:
        print("== Stored reports ==")
        ok = recheck_stored() and ok
    if not args.stored_only:
        print("\n== Checker self-tests ==")
        ok = run_selftests() and ok
    sys.exit(0 if ok else 1)