"""
bundle_decomposition.py
-----------------------
Stage 6a: what does a changed OTT bundle cost the customer?

Rules (from the workflow):
  * A dropped item gets a rupee value ONLY if a verified claim with a cited public price exists
    (field "ott_price_inr_per_month_<service>"). Otherwise it is labelled
    "unquantified value loss". Nothing is ever estimated.
  * Unverified price claims are ignored.
  * Sentences go through analytics.fact_checks.check_sentence plus one extra rule
    (check_rupee_rule): in a sentence that sits in the "Bundle value" section, or that cites an
    OTT price claim, every rupee figure must equal a VERIFIED OTT price claim cited by that
    same sentence. The plan's own price therefore cannot be passed off as an OTT price.
    The rule runs inside report_checker.check_report, so reports and cards both get it.
    Limitation: a sentence outside the "Bundle value" section that cites no OTT price claim
    is not covered by this rule (the normal number checks still apply).
  * We do not add prices together: a total would be a new number the checker cannot trace.

Run from the project root (after python -m analytics.seed_bundle_demo):
    python -m analytics.bundle_decomposition                   # all bundle changes
    python -m analytics.bundle_decomposition --change <id>
    python -m analytics.bundle_decomposition --selftest-only
"""

import argparse
import re
import sqlite3
import sys

from core.db import DB_PATH
from analytics.claim_store import get_change, get_claims_by_ids, parse_list, parse_number
from analytics.fact_checks import (
    build_citations,
    check_sentence,
    clean_claim_ids,
    fmt_num,
    number_matches,
)

PRICE_FIELD_PREFIX = "ott_price_inr_per_month_"
BUNDLE_SECTION = "Bundle value"
UNQUANTIFIED = "unquantified value loss"
_RUPEE_RE = re.compile(r"(?:\bRs\.?|\bINR|\u20b9)\s*(\d[\d,]*(?:\.\d+)?)", re.I)


def slugify(item: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", item.lower()).strip("_")


def price_field(item: str) -> str:
    return PRICE_FIELD_PREFIX + slugify(item)


def _key(item: str) -> str:
    return " ".join(item.lower().split())


def find_price_claim(market: str, item: str, as_of: str):
    """Latest VERIFIED cited price claim for this service in this market, at or before as_of."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT * FROM claims WHERE market = ? AND field = ? AND verified = 1 "
            "AND observed_at <= ? ORDER BY observed_at DESC, claim_id DESC LIMIT 1",
            (market, price_field(item), as_of),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def list_bundle_change_ids() -> list[str]:
    conn = sqlite3.connect(DB_PATH)
    try:
        rows = conn.execute("SELECT id FROM changes WHERE field = 'ott_bundle' ORDER BY id").fetchall()
    finally:
        conn.close()
    return [r[0] for r in rows]


def decompose(change_id: str):
    """Structured result, or None if the change is missing or is not an OTT bundle change."""
    change = get_change(change_id)
    if not change or change["field_canonical"] != "ott_bundle":
        return None

    old_items = parse_list(change["old_value"])
    new_items = parse_list(change["new_value"])
    old_keys = {_key(i) for i in old_items}
    new_keys = {_key(i) for i in new_items}

    dropped = []
    for item in old_items:
        if _key(item) in new_keys:
            continue
        claim = find_price_claim(change["market"], item, change["new_observed_at"])
        price = parse_number(claim["value"]) if claim else None
        if claim and price is not None and price > 0:
            dropped.append({"item": item, "status": "quantified",
                            "price_inr_per_month": price, "price_claim_id": claim["claim_id"]})
        else:
            dropped.append({"item": item, "status": UNQUANTIFIED,
                            "price_inr_per_month": None, "price_claim_id": None})

    return {
        "change_id": change_id, "market": change["market"], "competitor": change["competitor"],
        "plan_or_sku": change["plan_or_sku"],
        "claim_id_old": change["claim_id_old"], "claim_id_new": change["claim_id_new"],
        "old_items": old_items, "new_items": new_items, "dropped": dropped,
        "added": [i for i in new_items if _key(i) not in old_keys],
    }


def build_sentences(d: dict) -> list[dict]:
    comp, plan = d["competitor"], d["plan_or_sku"]
    base_ids = [d["claim_id_old"], d["claim_id_new"]]
    out = []

    def add(lane, text, ids):
        ids = list(dict.fromkeys(i for i in ids if i))
        out.append({"id": f"B{len(out) + 1}", "section": BUNDLE_SECTION, "lane": lane, "text": text,
                    "claim_ids": ids, "citations": build_citations(ids)})

    old_txt = ", ".join(d["old_items"]) or "no bundled services"
    new_txt = ", ".join(d["new_items"]) or "no bundled services"
    add("evidence", f'{comp} changed the OTT bundle of {plan} from "{old_txt}" to "{new_txt}".', base_ids)

    for x in d["dropped"]:
        item = x["item"]
        if x["status"] == "quantified":
            add("evidence",
                f"{comp} removed {item} from {plan}; a cited public price for {item} is "
                f"Rs {fmt_num(x['price_inr_per_month'])} per month.",
                base_ids + [x["price_claim_id"]])
        else:
            add("model",
                f"Removing {item} is an {UNQUANTIFIED}: no cited public price for {item} "
                "was found, so no rupee value is given.",
                base_ids)

    for item in d["added"]:
        add("evidence", f"{comp} added {item} to the OTT bundle of {plan}.", base_ids)
    return out


def check_rupee_rule(sentence: dict) -> list[dict]:
    """
    Bundle rule. Applies to a sentence in the "Bundle value" section, or any sentence that cites an
    OTT price claim. Every rupee figure in such a sentence must equal a VERIFIED OTT price claim
    cited by that same sentence. Returns [] when the rule does not apply or is satisfied.
    """
    if not isinstance(sentence, dict):
        return []
    text = sentence.get("text")
    if not isinstance(text, str):
        return []
    figures = _RUPEE_RE.findall(text)
    if not figures:
        return []

    found = get_claims_by_ids(clean_claim_ids(sentence.get("claim_ids")))
    in_bundle_section = sentence.get("section") == BUNDLE_SECTION
    cites_ott_price = any(c["field"].startswith(PRICE_FIELD_PREFIX) for c in found.values())
    if not (in_bundle_section or cites_ott_price):
        return []

    prices = []
    for c in found.values():
        if c["field"].startswith(PRICE_FIELD_PREFIX) and int(c.get("verified") or 0) == 1:
            p = parse_number(c["value"])
            if p is not None:
                prices.append(p)

    issues = []
    for token in figures:
        if not number_matches(token, prices):
            issues.append({
                "code": "rupee_not_a_cited_ott_price",
                "message": f"Rs {token} is not the value of a verified OTT price claim cited by this sentence.",
            })
    return issues


def check_bundle_sentence(sentence: dict) -> list[dict]:
    """The standard sentence check plus the rupee-figure rule. Empty list means it passes."""
    return list(check_sentence(sentence)) + check_rupee_rule(sentence)


# -- Self-tests ---------------------------------------------------------------

AW = "SIM-chg-isp-airwave-ott_bundle"
NS = "SIM-chg-isp-netstream-ott_bundle"
AW_OLD, AW_NEW = "SIM-isp-airwave-ott_bundle-T0", "SIM-isp-airwave-ott_bundle-T1"
NS_OLD, NS_NEW = "SIM-isp-netstream-ott_bundle-T0", "SIM-isp-netstream-ott_bundle-T1"
NS_PRICE = "SIM-isp-netstream-ott_price_inr_per_month_disney_hotstar-T0"
NS_PLAN_PRICE = "SIM-isp-netstream-price_inr_per_month-T0"
AW_DECOY = "SIM-isp-airwave-ott_price_inr_per_month_sonyliv-UNVERIFIED"


def _mk(lane, text, ids, section=None):
    s = {"id": "T1", "lane": lane, "text": text, "claim_ids": list(ids),
         "citations": build_citations(list(ids))}
    if section:
        s["section"] = section
    return s


def run_selftests() -> bool:
    if get_change(AW) is None or get_change(NS) is None:
        print("Self-tests need the bundle demo data. Run:\n  python -m analytics.seed_bundle_demo")
        return False

    aw, ns = decompose(AW), decompose(NS)
    total, good = 0, 0

    def record(name, behaved, outcome):
        nonlocal total, good
        total += 1
        good += 1 if behaved else 0
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")

    d = ns["dropped"][0] if ns and len(ns["dropped"]) == 1 else {}
    record("NetStream drop is quantified from the cited price claim",
           d.get("status") == "quantified" and d.get("price_inr_per_month") == 299.0
           and d.get("price_claim_id") == NS_PRICE, str(d.get("status")))

    d = aw["dropped"][0] if aw and len(aw["dropped"]) == 1 else {}
    record("AirWave drop stays unquantified despite an unverified price claim",
           d.get("status") == UNQUANTIFIED and d.get("price_claim_id") is None, str(d.get("status")))

    for label, dec in (("AirWave", aw), ("NetStream", ns)):
        bad = [(s["id"], i["code"]) for s in build_sentences(dec) for i in check_bundle_sentence(s)]
        record(f"generated {label} sentences all pass", not bad, "passed" if not bad else f"rejected {bad}")

    comparison_style = _mk("evidence", "NetStream Stream Pro: price Rs 699; OTT bundle Prime Video.",
                           [NS_PLAN_PRICE, NS_NEW], section="Facts as observed")
    issues = check_bundle_sentence(comparison_style)
    record("plan price next to a bundle fact is allowed (comparison style)", not issues,
           "passed" if not issues else "rejected " + ",".join(sorted({i["code"] for i in issues})))

    mutations = [
        ("invented OTT price is rejected",
         _mk("evidence", "NetStream removed Disney+ Hotstar from Stream Pro; a cited public price "
             "for Disney+ Hotstar is Rs 399 per month.", [NS_OLD, NS_NEW, NS_PRICE], BUNDLE_SECTION),
         "rupee_not_a_cited_ott_price"),
        ("plan price passed off as an OTT price is rejected",
         _mk("evidence", "NetStream removed Disney+ Hotstar from Stream Pro; a cited public price "
             "for Disney+ Hotstar is Rs 699 per month.", [NS_OLD, NS_NEW, NS_PLAN_PRICE], BUNDLE_SECTION),
         "rupee_not_a_cited_ott_price"),
        ("rupee figure on an unquantified item is rejected",
         _mk("model", "Removing SonyLIV is worth Rs 499 per month.", [AW_OLD, AW_NEW], BUNDLE_SECTION),
         "rupee_not_a_cited_ott_price"),
        ("unverified decoy price claim is rejected",
         _mk("evidence", "AirWave removed SonyLIV from Air Plus; a cited public price for SonyLIV "
             "is Rs 499 per month.", [AW_OLD, AW_NEW, AW_DECOY], BUNDLE_SECTION),
         "unverified_claim"),
        ("bundle sentence without a claim ID is rejected",
         _mk("model", f"Removing SonyLIV is an {UNQUANTIFIED}.", [], BUNDLE_SECTION),
         "no_claim_id"),
    ]
    for name, sentence, expect in mutations:
        codes = sorted({i["code"] for i in check_bundle_sentence(sentence)})
        record(name, expect in codes, "rejected " + ",".join(codes) if codes else "passed")

    from analytics.report_checker import check_report  # lazy import: report_checker imports this module

    plan_as_ott = _mk("evidence",
                      "NetStream removed Disney+ Hotstar from Stream Pro; a cited public price "
                      "for Disney+ Hotstar is Rs 699 per month.", [NS_OLD, NS_NEW, NS_PLAN_PRICE],
                      BUNDLE_SECTION)
    res = check_report({"sentences": [plan_as_ott], "origins": ["simulated"]})
    codes = sorted({i["code"] for i in res["issues"]})
    record("check_report enforces the rupee rule", "rupee_not_a_cited_ott_price" in codes,
           "rejected " + ",".join(codes) if codes else "passed")
    clean = check_report({"sentences": build_sentences(ns), "origins": ["simulated"]})
    record("check_report passes the generated NetStream sentences", clean["passed"],
           "passed" if clean["passed"] else f"rejected {[i['code'] for i in clean['issues']]}")

    print(f"Self-tests: {good}/{total} behaved as expected.")
    return good == total


def _show(change_id: str) -> bool:
    d = decompose(change_id)
    if d is None:
        print(f"{change_id}: not found, or not an OTT bundle change.")
        return False
    print(f"\n== {change_id} ==")
    print(f"{d['competitor']} {d['plan_or_sku']}: bundle change")
    for x in d["dropped"]:
        if x["status"] == "quantified":
            print(f"  dropped: {x['item']} -> Rs {fmt_num(x['price_inr_per_month'])}/month "
                  f"(cited: {x['price_claim_id']})")
        else:
            print(f"  dropped: {x['item']} -> {UNQUANTIFIED}")
    print("  added: " + (", ".join(d["added"]) or "(none)"))
    ok = True
    for s in build_sentences(d):
        issues = check_bundle_sentence(s)
        ok = ok and not issues
        print(f"  [{'PASSED' if not issues else 'REJECTED'}] {s['id']} ({s['lane']} lane) {s['text']}")
        print("      cites " + ", ".join(s["claim_ids"]))
        for i in issues:
            print(f"      {i['code']}: {i['message']}")
    return ok


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 6a bundle decomposition")
    parser.add_argument("--change", help="one bundle change ID")
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()

    ok = True
    if not args.selftest_only:
        ids = [args.change] if args.change else list_bundle_change_ids()
        if not ids:
            print("No bundle changes found. Run: python -m analytics.seed_bundle_demo")
        for cid in ids:
            ok = _show(cid) and ok
    if not args.change:
        print("\n== Bundle self-tests ==")
        ok = run_selftests() and ok
    sys.exit(0 if ok else 1)