"""
card_checker.py
---------------
Deterministic checker for Counter-Move cards (no LLM). A card passes only if

  * owner is a non-empty string, status is valid, card_type is threat|opportunity
  * expires_at parses and is after created_at
  * the change exists, and card_type matches the trigger stored by Stage 3
  * facts / impact / ideas each hold at least one sentence
  * facts are evidence-lane; impact and ideas are model-lane
  * every sentence passes analytics.fact_checks.check_sentence
    (claim IDs exist and are verified, citations match the DB, numbers come from the facts)
  * every idea starts with "Idea:", has kind "idea", and contains no overclaim words
  * origins of the cited claims (for example "simulated") are declared on the card

Run from the project root:
    python -m analytics.card_checker                  # re-check stored cards + self-tests
    python -m analytics.card_checker --stored-only
    python -m analytics.card_checker --selftest-only
"""

import argparse
import copy
import re
import sys
from datetime import datetime, timedelta, timezone

from analytics.claim_store import CARD_STATUSES, get_cards, get_change
from analytics.fact_checks import build_citations
from analytics.live_analysis import get_analysis
from analytics.report_checker import check_report

CARD_TYPES = ("threat", "opportunity")
GROUP_LANES = {"facts": "evidence", "impact": "model", "ideas": "model"}
OVERCLAIM_RE = re.compile(
    r"\b(guarantee[sd]?|will (?:win|gain|increase|boost|capture)|proven|certain|definitely|always|never)\b",
    re.I,
)


def _parse_ts(value):
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def check_card(card: dict) -> dict:
    """{"passed": bool, "issues": [{sentence_id, code, message}], "sentences_checked": int}"""
    issues = []

    def add(code, message, sid=None):
        issues.append({"sentence_id": sid, "code": code, "message": message})

    owner = card.get("owner")
    if not isinstance(owner, str) or not owner.strip():
        add("owner_missing", "Card has no owner.")
    if card.get("status") not in CARD_STATUSES:
        add("bad_status", f"Status must be one of {sorted(CARD_STATUSES)}, got {card.get('status')!r}.")

    created, expires = _parse_ts(card.get("created_at")), _parse_ts(card.get("expires_at"))
    if created is None:
        add("bad_created_at", "created_at is missing or not an ISO timestamp.")
    if expires is None:
        add("bad_expiry", "expires_at is missing or not an ISO timestamp.")
    if created and expires and expires <= created:
        add("expiry_not_after_creation", "expires_at must be later than created_at.")

    ctype = card.get("card_type")
    if ctype not in CARD_TYPES:
        add("bad_card_type", f"card_type must be one of {CARD_TYPES}, got {ctype!r}.")

    change_id = card.get("change_id") or ""
    if get_change(change_id) is None:
        add("unknown_change", f"Change {change_id!r} does not exist (or its claims are missing).")
    else:
        analysis = get_analysis(change_id)
        if analysis and analysis.get("card_trigger") != ctype:
            add("trigger_mismatch",
                f"Card type {ctype!r} does not match the stored trigger {analysis.get('card_trigger')!r}.")

    all_sentences = []
    for group, lane in GROUP_LANES.items():
        items = card.get(group)
        if not isinstance(items, list) or not items:
            add("empty_group", f"Card needs at least one sentence in '{group}'.")
            continue
        for s in items:
            if isinstance(s, dict) and s.get("lane") != lane:
                add("wrong_lane_for_group", f"'{group}' sentences must be {lane}-lane.", s.get("id"))
        all_sentences += items

    if all_sentences:
        sub = check_report({"sentences": all_sentences, "origins": card.get("origins")})
        issues += sub["issues"]

    for s in card.get("ideas") or []:
        if not isinstance(s, dict):
            continue
        text = s.get("text") or ""
        if not text.startswith("Idea:") or s.get("kind") != "idea":
            add("idea_not_labelled", "Ideas must start with 'Idea:' and have kind 'idea'.", s.get("id"))
        match = OVERCLAIM_RE.search(text)
        if match:
            add("overclaim", f"Idea uses overclaiming wording '{match.group(0)}'.", s.get("id"))

    return {"passed": not issues, "issues": issues, "sentences_checked": len(all_sentences)}


def card_view(row: dict) -> dict:
    """A stored card row as one dict. The table columns are authoritative for owner/expiry/status."""
    return {**row["body"], "card_id": row["id"], "owner": row["owner"],
            "expires_at": row["expires_at"], "status": row["status"]}


def recheck_stored() -> bool:
    rows = get_cards(limit=1000)
    if not rows:
        print("No stored cards yet. Run: python -m analytics.card_builder")
        return True
    all_ok = True
    for row in rows:
        result = check_card(card_view(row))
        status = "PASSED" if result["passed"] else "REJECTED"
        print(f"[{status}] {row['id']} ({row['status']}): {result['sentences_checked']} sentences")
        for i in result["issues"]:
            print(f"    {i['sentence_id']} {i['code']}: {i['message']}")
        all_ok = all_ok and result["passed"]
    return all_ok


# ── Self-tests ───────────────────────────────────────────────────────────────

CHANGE = "SIM-chg-isp-speedfiber-fup_cap_gb"
A_OLD, A_NEW = "SIM-isp-speedfiber-fup_cap_gb-T0", "SIM-isp-speedfiber-fup_cap_gb-T1"
P_OLD, P_NEW = "SIM-isp-speedfiber-price_inr_per_month-T0", "SIM-isp-speedfiber-price_inr_per_month-T1"
DECOY = "SIM-isp-bharatlink-speed_mbps-UNVERIFIED"


def _mk(sid, lane, text, ids, kind=None):
    s = {"id": sid, "lane": lane, "text": text, "claim_ids": list(ids), "citations": build_citations(ids)}
    if kind:
        s["kind"] = kind
    return s


def _base_card() -> dict:
    now = datetime.now(timezone.utc)
    return {
        "card_type": "opportunity", "change_id": CHANGE,
        "created_at": now.isoformat(), "expires_at": (now + timedelta(days=7)).isoformat(),
        "owner": "Pricing Lead", "status": "pending", "origins": ["simulated"],
        "facts": [_mk("F1", "evidence",
                      "SpeedFiber changed the FUP data cap of Fiber Ultra from 3300 GB to 1500 GB.",
                      [A_OLD, A_NEW])],
        "impact": [_mk("I1", "model",
                       "Cost per GB (before FUP) rose from 0.2421 to 0.5327 Rs/GB (+120.00%), "
                       "which is worse for the customer.", [A_OLD, A_NEW, P_OLD, P_NEW])],
        "ideas": [_mk("G1", "model",
                      "Idea: compare your own FUP data cap with SpeedFiber's FUP data cap, which is now "
                      "1500 GB, and decide whether the difference is worth raising with customers who value it.",
                      [A_NEW], kind="idea")],
    }


def _tests():
    return [
        ("valid card passes", lambda c: None, None),
        ("missing owner is rejected", lambda c: c.pop("owner"), "owner_missing"),
        ("blank owner is rejected", lambda c: c.__setitem__("owner", "   "), "owner_missing"),
        ("expiry before creation is rejected",
         lambda c: c.__setitem__("expires_at",
                                 (datetime.fromisoformat(c["created_at"]) - timedelta(days=1)).isoformat()),
         "expiry_not_after_creation"),
        ("unparseable expiry is rejected", lambda c: c.__setitem__("expires_at", "next week"), "bad_expiry"),
        ("invalid status is rejected", lambda c: c.__setitem__("status", "sent"), "bad_status"),
        ("idea without claim ID is rejected",
         lambda c: c["ideas"][0].__setitem__("claim_ids", []), "no_claim_id"),
        ("idea with an invented number is rejected",
         lambda c: c["ideas"][0].__setitem__("text", "Idea: offer a Rs 500 discount to SpeedFiber's customers."),
         "number_not_in_facts"),
        ("idea without the Idea: label is rejected",
         lambda c: c["ideas"][0].__setitem__("text", "Compare your plan with SpeedFiber's, which is now 1500 GB."),
         "idea_not_labelled"),
        ("overclaiming idea is rejected",
         lambda c: c["ideas"][0].__setitem__(
             "text", "Idea: raise your FUP data cap, which will definitely win customers from SpeedFiber's 1500 GB plan."),
         "overclaim"),
        ("fact sentence in the model lane is rejected",
         lambda c: c["facts"][0].__setitem__("lane", "model"), "wrong_lane_for_group"),
        ("card type that disagrees with the Stage 3 trigger is rejected",
         lambda c: c.__setitem__("card_type", "threat"), "trigger_mismatch"),
        ("unknown change ID is rejected", lambda c: c.__setitem__("change_id", "SIM-chg-nope"), "unknown_change"),
        ("card with no ideas is rejected", lambda c: c.__setitem__("ideas", []), "empty_group"),
        ("card that hides the simulated label is rejected",
         lambda c: c.__setitem__("origins", []), "origin_label_missing"),
        ("unverified claim in the impact is rejected",
         lambda c: c["impact"][0].update(claim_ids=[DECOY], citations=build_citations([DECOY])),
         "unverified_claim"),
    ]


def run_selftests() -> bool:
    analysis = get_analysis(CHANGE)
    if get_change(CHANGE) is None or not analysis or analysis.get("card_trigger") != "opportunity":
        print("Self-tests need the seeded claims and Stage 3 analysis. Run:\n"
              "  python -m analytics.seed_demo_claims\n  python -m analytics.live_analysis")
        return False

    tests = _tests()
    good = 0
    for name, mutate, expect in tests:
        card = copy.deepcopy(_base_card())
        mutate(card)
        issues = check_card(card)["issues"]
        codes = sorted({i["code"] for i in issues})
        behaved = (not issues) if expect is None else (expect in codes)
        good += 1 if behaved else 0
        outcome = "passed" if not issues else "rejected " + ",".join(codes)
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")
    print(f"Self-tests: {good}/{len(tests)} behaved as expected.")
    return good == len(tests)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Counter-Move card checker")
    parser.add_argument("--stored-only", action="store_true")
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()

    ok = True
    if not args.selftest_only:
        print("== Stored cards ==")
        ok = recheck_stored() and ok
    if not args.stored_only:
        print("\n== Card checker self-tests ==")
        ok = run_selftests() and ok
    sys.exit(0 if ok else 1)