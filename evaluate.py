"""
evaluate.py
-----------
Benchmark harness for Person 2's deliverables.

Generates ~40 mutated page variants (real changes + noise),
runs both a naive text-diff baseline and RivalIQ, and reports:
  - Alerts raised
  - Precision (of alerts raised, how many were real)
  - Recall    (of real changes, how many were caught)
  - Stealth-change recall
  - Extraction-verified %
  - Time-to-detect (simulated as pipeline latency)

Run:
    python evaluate.py
"""

import json
import time
import hashlib
from datetime import datetime, timezone
from extraction.extractor import extract_claims
from verification.verifier import verify_claims
from change_intel.differ import _diff_claim_sets, _load_claims
from change_intel.classifier import classify_diffs

# ── Synthetic test cases ──────────────────────────────────────────────────────

def _make_isp_baseline() -> str:
    return (
        "SpeedFiber Fiber Ultra Plan: Price ₹799/month. "
        "Speed: 200 Mbps. FUP Cap: 3300 GB. Post-FUP Speed: 10 Mbps. "
        "Included OTT: Disney+ Hotstar, SonyLIV. "
        "We use cookies to enhance your browsing experience. "
        "© 2026 SpeedFiber Broadband Pune. All rights reserved. "
        '"Fastest internet in Pune! - Rahul S."'
    )

def _make_dairy_baseline() -> str:
    return (
        "FreshDairy Fresh Whole Milk: ₹30. "
        "Pack Volume: 500 ml. Fat Content: 4.5%. "
        "We use cookies. © 2026 FreshDairy Ltd. "
        '"Pure and fresh daily milk."'
    )

def _make_headphones_baseline() -> str:
    return (
        "SoundMax Pro TWS: ₹1,999. "
        "Playtime: Up to 40 Hours. "
        "Active Noise Cancellation: Yes. Warranty: 12 Months. "
        "We use cookies. © 2026 SoundMax Audio India. "
        '"Amazing noise cancellation!"'
    )


BASELINE_FACTORY = {
    "isp": _make_isp_baseline,
    "dairy": _make_dairy_baseline,
    "headphones": _make_headphones_baseline,
}

# (label, market, mutated_text, is_real_change)
TEST_CASES = [
    # ── ISP ──────────────────────────────────────────────────────────────────
    ("isp_price_hike",         "isp",
     _make_isp_baseline().replace("₹799", "₹899"), True),
    ("isp_fup_cut_stealth",    "isp",
     _make_isp_baseline().replace("3300 GB", "1500 GB"), True),
    ("isp_speed_cut",          "isp",
     _make_isp_baseline().replace("200 Mbps", "150 Mbps"), True),
    ("isp_ott_removed",        "isp",
     _make_isp_baseline().replace("Disney+ Hotstar, SonyLIV", "SonyLIV"), True),
    ("isp_new_ott_added",      "isp",
     _make_isp_baseline().replace("SonyLIV", "SonyLIV, Zee5"), True),
    ("isp_post_fup_cut",       "isp",
     _make_isp_baseline().replace("10 Mbps", "5 Mbps"), True),
    ("isp_plan_renamed",       "isp",
     _make_isp_baseline().replace("Fiber Ultra", "Fiber Max"), True),
    # Noise
    ("isp_footer_year",        "isp",
     _make_isp_baseline().replace("2026", "2027"), False),
    ("isp_cookie_banner",      "isp",
     _make_isp_baseline().replace(
         "We use cookies to enhance your browsing experience.",
         "Updated Privacy Policy: We value your data security."), False),
    ("isp_testimonial",        "isp",
     _make_isp_baseline().replace(
         "Fastest internet in Pune! - Rahul S.",
         "Great connectivity across central Pune!"), False),
    ("isp_whitespace",         "isp",
     _make_isp_baseline().replace("₹799/month", "₹799 / month"), False),

    # ── Dairy ─────────────────────────────────────────────────────────────────
    ("dairy_price_hike",       "dairy",
     _make_dairy_baseline().replace("₹30", "₹32"), True),
    ("dairy_pack_shrink",      "dairy",
     _make_dairy_baseline().replace("500 ml", "450 ml"), True),
    ("dairy_fat_cut",          "dairy",
     _make_dairy_baseline().replace("4.5%", "3.5%"), True),
    ("dairy_name_change",      "dairy",
     _make_dairy_baseline().replace("Fresh Whole Milk", "Premium Whole Milk"), True),
    ("dairy_price_and_pack",   "dairy",
     _make_dairy_baseline().replace("₹30", "₹32").replace("500 ml", "450 ml"), True),
    ("dairy_price_cut",        "dairy",
     _make_dairy_baseline().replace("₹30", "₹28"), True),
    # Noise
    ("dairy_footer_year",      "dairy",
     _make_dairy_baseline().replace("2026", "2027"), False),
    ("dairy_cookie_banner",    "dairy",
     _make_dairy_baseline().replace("We use cookies.", "Privacy updated."), False),
    ("dairy_testimonial",      "dairy",
     _make_dairy_baseline().replace(
         "Pure and fresh daily milk.", "Freshness delivered every morning."), False),
    ("dairy_caps",             "dairy",
     _make_dairy_baseline().replace("Fat Content", "FAT CONTENT"), False),

    # ── Headphones ────────────────────────────────────────────────────────────
    ("hp_price_hike",          "headphones",
     _make_headphones_baseline().replace("₹1,999", "₹2,299"), True),
    ("hp_battery_cut_stealth", "headphones",
     _make_headphones_baseline().replace("40 Hours", "30 Hours"), True),
    ("hp_anc_removed",         "headphones",
     _make_headphones_baseline().replace(
         "Active Noise Cancellation: Yes", "Active Noise Cancellation: No"), True),
    ("hp_warranty_cut",        "headphones",
     _make_headphones_baseline().replace("12 Months", "6 Months"), True),
    ("hp_model_renamed",       "headphones",
     _make_headphones_baseline().replace("SoundMax Pro TWS", "SoundMax Elite TWS"), True),
    ("hp_price_and_battery",   "headphones",
     _make_headphones_baseline().replace("₹1,999", "₹2,199").replace("40 Hours", "35 Hours"), True),
    ("hp_price_cut",           "headphones",
     _make_headphones_baseline().replace("₹1,999", "₹1,799"), True),
    # Noise
    ("hp_footer_year",         "headphones",
     _make_headphones_baseline().replace("2026", "2027"), False),
    ("hp_cookie_banner",       "headphones",
     _make_headphones_baseline().replace(
         "We use cookies.", "Privacy Policy updated."), False),
    ("hp_testimonial",         "headphones",
     _make_headphones_baseline().replace(
         "Amazing noise cancellation!", "Crystal clear bass and highs!"), False),
    ("hp_whitespace",          "headphones",
     _make_headphones_baseline().replace("₹1,999", "₹ 1,999"), False),
]

SOURCE_META = {
    "isp":        ("src_eval_isp",  "SpeedFiber", "http://localhost:8001/isp/plans",    "Pune"),
    "dairy":      ("src_eval_dairy","FreshDairy",  "http://localhost:8001/dairy/products","Pune"),
    "headphones": ("src_eval_hp",   "SoundMax",    "http://localhost:8001/audio/earbuds", "Pune"),
}


def _naive_baseline_alerts(baseline_text: str, mutated_text: str) -> int:
    """Naive: alert on ANY text change."""
    return 0 if baseline_text.strip() == mutated_text.strip() else 1


def run_evaluation():
    results = []
    total = len(TEST_CASES)
    real_changes = [tc for tc in TEST_CASES if tc[3]]
    noise_cases  = [tc for tc in TEST_CASES if not tc[3]]

    naive_tp = naive_fp = rivaliq_tp = rivaliq_fp = 0
    rivaliq_fn = naive_fn = 0
    stealth_total = stealth_caught = 0
    extraction_total = extraction_verified = 0

    for label, market, mutated_text, is_real in TEST_CASES:
        baseline_fn  = BASELINE_FACTORY[market]
        baseline_text = baseline_fn()
        src_id, competitor, url, region = SOURCE_META[market]

        # ── Naive baseline ────────────────────────────────────────────────
        naive_alerted = _naive_baseline_alerts(baseline_text, mutated_text) == 1

        # ── RivalIQ pipeline ──────────────────────────────────────────────
        t_start = time.monotonic()

        # Extract + verify baseline
        base_claims_raw = extract_claims(
            "snap_eval_base", src_id, market, competitor,
            baseline_text, url, region
        )
        base_claims = verify_claims(base_claims_raw, baseline_text)
        base_keyed = {(c["plan_or_sku"], c["field"]): c for c in base_claims if c["verified"]}

        # Extract + verify mutated
        mut_claims_raw = extract_claims(
            "snap_eval_mut", src_id, market, competitor,
            mutated_text, url, region
        )
        mut_claims = verify_claims(mut_claims_raw, mutated_text)
        mut_keyed = {(c["plan_or_sku"], c["field"]): c for c in mut_claims if c["verified"]}

        extraction_total += len(mut_claims_raw)
        extraction_verified += sum(1 for c in mut_claims if c["verified"])

        # Diff
        diffs = _diff_claim_sets(base_keyed, mut_keyed)

        # Classify (no DB writes during eval — pass empty competitor)
        changes = classify_diffs(diffs, market=market, competitor="")
        rivaliq_alerted = len([c for c in changes if c["severity"] in ("high","medium")]) > 0

        # Stealth
        stealth_in_changes = [c for c in changes if c["is_stealth"]]
        if is_real and "stealth" in label:
            stealth_total += 1
            if stealth_in_changes:
                stealth_caught += 1

        t_elapsed_ms = round((time.monotonic() - t_start) * 1000, 1)

        # ── Score ─────────────────────────────────────────────────────────
        if is_real:
            if naive_alerted:  naive_tp  += 1
            else:              naive_fn  += 1
            if rivaliq_alerted: rivaliq_tp += 1
            else:               rivaliq_fn += 1
        else:
            if naive_alerted:  naive_fp  += 1
            if rivaliq_alerted: rivaliq_fp += 1

        results.append({
            "label": label,
            "market": market,
            "is_real_change": is_real,
            "naive_alerted": naive_alerted,
            "rivaliq_alerted": rivaliq_alerted,
            "diffs_found": len(diffs),
            "changes_classified": len(changes),
            "stealth_flagged": len(stealth_in_changes) > 0,
            "ttd_ms": t_elapsed_ms,
        })

    # ── Metrics ────────────────────────────────────────────────────────────
    def safe_div(a, b): return round(a / b, 3) if b else 0.0

    naive_precision  = safe_div(naive_tp,   naive_tp  + naive_fp)
    naive_recall     = safe_div(naive_tp,   naive_tp  + naive_fn)
    rivaliq_precision = safe_div(rivaliq_tp, rivaliq_tp + rivaliq_fp)
    rivaliq_recall    = safe_div(rivaliq_tp, rivaliq_tp + rivaliq_fn)
    stealth_recall    = safe_div(stealth_caught, stealth_total)
    extraction_pct    = safe_div(extraction_verified, extraction_total) * 100

    print("\n" + "="*60)
    print("RivalIQ Evaluation Results")
    print("="*60)
    print(f"Total test cases : {total}  (real={len(real_changes)}, noise={len(noise_cases)})")
    print()
    print(f"{'Metric':<35} {'Naive':>10} {'RivalIQ':>10}")
    print("-"*55)
    print(f"{'Alerts raised':<35} {naive_tp+naive_fp:>10} {rivaliq_tp+rivaliq_fp:>10}")
    print(f"{'True positives':<35} {naive_tp:>10} {rivaliq_tp:>10}")
    print(f"{'False positives':<35} {naive_fp:>10} {rivaliq_fp:>10}")
    print(f"{'Precision':<35} {naive_precision:>10.1%} {rivaliq_precision:>10.1%}")
    print(f"{'Recall':<35} {naive_recall:>10.1%} {rivaliq_recall:>10.1%}")
    print()
    print(f"Stealth-change recall : {stealth_caught}/{stealth_total} = {stealth_recall:.1%}")
    print(f"Extraction verified % : {extraction_verified}/{extraction_total} = {extraction_pct:.1f}%")
    avg_ttd = sum(r["ttd_ms"] for r in results) / len(results)
    print(f"Avg time-to-detect    : {avg_ttd:.1f} ms")
    print("="*60)

    # Save JSON
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total": total, "real": len(real_changes), "noise": len(noise_cases),
            "naive":   {"precision": naive_precision,   "recall": naive_recall,
                        "tp": naive_tp,  "fp": naive_fp,  "fn": naive_fn},
            "rivaliq": {"precision": rivaliq_precision, "recall": rivaliq_recall,
                        "tp": rivaliq_tp, "fp": rivaliq_fp, "fn": rivaliq_fn},
            "stealth_recall": stealth_recall,
            "extraction_verified_pct": round(extraction_pct, 1),
            "avg_ttd_ms": round(avg_ttd, 1),
        },
        "cases": results,
    }
    with open("data/eval_results.json", "w") as f:
        json.dump(output, f, indent=2)
    print("\nResults saved to data/eval_results.json")


if __name__ == "__main__":
    from core.db import init_db
    import os
    os.makedirs("data", exist_ok=True)
    init_db()
    run_evaluation()