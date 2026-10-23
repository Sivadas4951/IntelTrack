"""
vis_score.py
------------
Stage 6b: Value Integrity Score (VIS), a model-lane composite.

  VIS = 100 - (w_p*P + w_q*Q + w_b*B), clamped at 0.
  P = unexplained effective-price increase (0-100), from the raw price field.
  Q = reduction in included quantity/quality (0-100): the worst-hit numeric,
      higher-is-better field other than price for this market.
  B = reduction in bundled benefits (0-100): cited-price OTT drops only, as a
      percent of the plan's own price. An unquantified drop (no cited price)
      adds nothing to B -- see bundle_unquantified; the sentence must say so.
  Weights come from config/value_rules.yaml (markets.<m>.weights).

Scope: VIS is computed PER CHANGE EVENT. B only reflects a bundle drop when the
triggering change IS the ott_bundle field itself.

Every VIS sentence must say "composite model, not an observed fact" and must
set sentence["is_vis"] = True so the checker (fact_checks.py) allows the
P/Q/B/VIS numbers and weights for that sentence only.

Run from the project root (after seeding):
    python -m analytics.vis_score --change <change_id>
    python -m analytics.vis_score --selftest-only
"""

import argparse
from datetime import datetime, timedelta, timezone

from analytics.bundle_decomposition import decompose as decompose_bundle
from analytics.claim_store import get_profiles, get_recent_changes, make_metric_name, save_metric
from analytics.config_loader import get_market_rules
from analytics.fact_checks import build_citations, check_sentence, fmt_num
from analytics.value_engine import analyze_change
from analytics.value_functions import vis_components

FIELD_LABELS_FALLBACK = {
    "speed_mbps": "speed", "fup_cap_gb": "FUP data cap", "post_fup_speed_mbps": "post-FUP speed",
    "pack_size_ml": "pack size", "fat_percentage": "fat content",
    "battery_hours": "battery life", "warranty_months": "warranty",
}


def _just_before(ts: str) -> str:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - timedelta(microseconds=1)).isoformat()


def _find_profile(profiles, plan):
    for p in profiles:
        if p["plan_or_sku"] == plan:
            return p
    return None


def _fv(profile, field):
    fact = (profile or {}).get("facts", {}).get(field)
    if fact is None:
        return None, None
    return fact["value"], fact["claim_id"]


def vis_for_change(change_id: str) -> dict | None:
    """Computes VIS for the change's plan, as of the change's own timestamp."""
    analysis = analyze_change(change_id)
    if analysis is None:
        return None
    fl = analysis["fact_layer"]
    market, comp, plan = fl["market"], fl["competitor"], fl["plan_or_sku"]
    rules = get_market_rules(market)
    after_ts = fl["new_observed_at"]

    after_profile = _find_profile(
        get_profiles(market, competitor=comp, as_of=after_ts, headline_only=True), plan)
    before_profile = _find_profile(
        get_profiles(market, competitor=comp, as_of=_just_before(after_ts), headline_only=True), plan)
    if after_profile is None:
        return None

    price_before, price_before_id = _fv(before_profile, "price")
    price_after, price_after_id = _fv(after_profile, "price")

    quantity_moves, quantity_ids = [], {}
    for field, ftype in rules.get("field_types", {}).items():
        if ftype != "number" or field == "price":
            continue
        if rules.get("field_direction", {}).get(field) != "higher_is_better":
            continue
        b_val, b_id = _fv(before_profile, field)
        a_val, a_id = _fv(after_profile, field)
        quantity_moves.append((field, b_val, a_val))
        quantity_ids[field] = (b_id, a_id)

    bundle_dropped, bundle_ids = [], []
    if fl["field_canonical"] == "ott_bundle":
        decomp = decompose_bundle(change_id)
        if decomp:
            bundle_dropped = decomp["dropped"]
            bundle_ids = [fl["claim_id_old"], fl["claim_id_new"]] + [
                x["price_claim_id"] for x in bundle_dropped if x.get("price_claim_id")]

    comp_result = vis_components(
        rules.get("weights", {}), price_before, price_after, quantity_moves, bundle_dropped)

    # Price is ALWAYS cited when known, even if P == 0 -- B's denominator depends on it,
    # and the checker must be able to see the same claims the generator used.
    ids = [price_before_id, price_after_id]
    if comp_result["q_field"]:
        ids += list(quantity_ids.get(comp_result["q_field"], (None, None)))
    ids += bundle_ids
    if not any(ids):
        ids = [fl["claim_id_old"], fl["claim_id_new"]]
    ids = [i for i in dict.fromkeys(ids) if i]

    return {
        "change_id": change_id, "market": market, "competitor": comp, "plan_or_sku": plan,
        "computed_at": after_ts, **comp_result, "claim_ids": ids,
        "bundle_dropped": bundle_dropped,
    }


def save_vis_metric(result: dict) -> str | None:
    if result is None:
        return None
    name = make_metric_name("vis", result["plan_or_sku"])
    return save_metric(
        result["market"], result["competitor"], name, result["vis"],
        result["claim_ids"], computed_at=result["computed_at"],
    )


def build_vis_sentences(result: dict) -> list[dict]:
    """[{"section","lane","text","claim_ids"}, ...]. Caller sets is_vis=True when wrapping."""
    if result is None:
        return []
    comp, plan = result["competitor"], result["plan_or_sku"]
    w = result["weights"]
    p_label = "price" if result["p_field"] else None
    q_label = FIELD_LABELS_FALLBACK.get(result["q_field"], result["q_field"]) if result["q_field"] else None

    parts = [
        f"P={fmt_num(result['p'])} (unexplained price increase"
        + (f", driven by {p_label}" if p_label else ", none found") + ")",
        f"Q={fmt_num(result['q'])} (quantity/quality reduction"
        + (f", driven by {q_label}" if q_label else ", none found") + ")",
        f"B={fmt_num(result['b'])} (bundle benefit reduction)",
    ]
    text = (f"{comp} {plan} Value Integrity Score (VIS) is {fmt_num(result['vis'])} out of 100 "
            f"(composite model, not an observed fact): VIS = 100 - "
            f"({fmt_num(w['price'])}*P + {fmt_num(w['quantity'])}*Q + {fmt_num(w['bundle'])}*B), "
            f"with {'; '.join(parts)}.")
    out = [{"section": "Value Integrity Score", "lane": "model", "text": text,
            "claim_ids": result["claim_ids"]}]

    if result["bundle_unquantified"]:
        lost_items = ", ".join(x["item"] for x in result["bundle_dropped"] if x.get("status") != "quantified")
        text2 = (f"VIS excludes an unquantified bundle loss ({lost_items} removed with no cited price); "
                 "the score above is a floor, not the full picture.")
        out.append({"section": "Value Integrity Score", "lane": "model", "text": text2,
                    "claim_ids": result["claim_ids"]})

    return out


def run_selftests() -> bool:
    good, total = 0, 0

    def record(name, behaved, outcome):
        nonlocal good, total
        total += 1
        good += 1 if behaved else 0
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")

    r_ns = vis_for_change("SIM-chg-isp-netstream-ott_bundle")
    record("NetStream VIS computes", r_ns is not None, str(r_ns and r_ns["vis"]))
    if r_ns:
        record("NetStream bundle B > 0 (cited price exists)", r_ns["b"] > 0, str(r_ns["b"]))
        record("NetStream has no unquantified bundle flag", not r_ns["bundle_unquantified"],
               str(r_ns["bundle_unquantified"]))

    r_aw = vis_for_change("SIM-chg-isp-airwave-ott_bundle")
    record("AirWave VIS computes", r_aw is not None, str(r_aw and r_aw["vis"]))
    if r_aw:
        record("AirWave flags an unquantified bundle loss", r_aw["bundle_unquantified"],
               str(r_aw["bundle_unquantified"]))

    r_sf = vis_for_change("SIM-chg-isp-speedfiber-fup_cap_gb")
    record("SpeedFiber VIS computes", r_sf is not None, str(r_sf and r_sf["vis"]))
    if r_sf:
        record("SpeedFiber Q > 0 (FUP cut)", r_sf["q"] > 0, str(r_sf["q"]))
        record("SpeedFiber B == 0 (no bundle change in this event)", r_sf["b"] == 0, str(r_sf["b"]))

    for name, r in (("NetStream", r_ns), ("AirWave", r_aw), ("SpeedFiber", r_sf)):
        if r is None:
            continue
        bad = []
        for s in build_vis_sentences(r):
            s = dict(s)
            s["id"] = "T"
            s["is_vis"] = True
            s["citations"] = build_citations(s["claim_ids"])
            bad += [(s["id"], i["code"]) for i in check_sentence(s)]
        record(f"{name} VIS sentences pass the checker", not bad, "passed" if not bad else f"rejected {bad}")

    print(f"Self-tests: {good}/{total} behaved as expected.")
    return good == total


def _show(change_id: str):
    r = vis_for_change(change_id)
    if r is None:
        print(f"{change_id}: not found or could not be analysed.")
        return
    print(f"\n== {change_id} ==")
    print(f"{r['competitor']} {r['plan_or_sku']}: VIS = {fmt_num(r['vis'])} "
          f"(P={fmt_num(r['p'])}, Q={fmt_num(r['q'])}, B={fmt_num(r['b'])})")
    print(f"  claim_ids: {r['claim_ids']}")
    for s in build_vis_sentences(r):
        print(f"  [{s['section']}] ({s['lane']}) {s['text']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 6b Value Integrity Score")
    parser.add_argument("--change", help="one change ID")
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()

    if args.change:
        _show(args.change)
    else:
        if not args.selftest_only:
            for ch in get_recent_changes(hours=24 * 30):
                _show(ch["change_id"])
        print("\n== VIS self-tests ==")
        run_selftests()