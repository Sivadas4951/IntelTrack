"""
card_builder.py
---------------
Stage 5: Counter-Move card generator. A card is built only for changes that Stage 3
flagged (high severity, clear direction). It never sends anything: the founder
approves or dismisses it.

  facts   evidence-lane sentences (copied from the checked change report)
  impact  model-lane sentences calculated from those facts (metrics, ranking shifts)
  ideas   templated, hedged suggestions labelled "Idea:"; they cite the claims that
          motivate them and may contain no number that is not in those claims

Owner and expiry come from config/value_rules.yaml (cards: default_owner, default_expiry_days).
Every card is run through analytics.card_checker before it is saved.

Run from the project root:
    python -m analytics.card_builder                     # build cards for pending triggers
    python -m analytics.card_builder --show              # ... and print them
    python -m analytics.card_builder --change <id> --show
    python -m analytics.card_builder --list
    python -m analytics.card_builder --approve <card_id>
    python -m analytics.card_builder --dismiss <card_id>
    python -m analytics.card_builder --backdate <card_id>   # TEST HOOK: makes the card already expired
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from analytics.card_checker import card_view, check_card
from analytics.claim_store import (
    expire_stale_cards,
    get_card,
    get_cards,
    save_card,
    update_card_status,
)
from analytics.config_loader import get_card_defaults
from analytics.fact_checks import build_citations
from analytics.live_analysis import attach_card, get_analysis, get_pending_card_triggers
from analytics.report_builder import _label, _with_unit, build_change_report
from analytics.value_engine import analyze_change

APPROVAL_NOTE = "Nothing is sent or changed automatically. Approve or dismiss this card in the dashboard."
LANE_NOTE = ("Facts quote verified claims. Impact is calculated from those facts (model lane). "
             "Ideas are suggestions, not facts.")


def _idea(index: int, text: str, claim_ids: list[str]) -> dict:
    ids = list(dict.fromkeys(i for i in claim_ids if i))
    return {"id": f"G{index}", "kind": "idea", "lane": "model", "text": text,
            "claim_ids": ids, "citations": build_citations(ids)}


def _copy_group(report: dict, prefix: str, sections: set, lane: str | None = None) -> list[dict]:
    out = []
    for s in report["sentences"]:
        if s["section"] in sections and (lane is None or s["lane"] == lane):
            item = dict(s)
            item["id"] = f"{prefix}{len(out) + 1}"
            out.append(item)
    return out


def build_card(change_id: str, trigger: str):
    """Returns (card, problems). card is None when it cannot be built safely."""
    built = build_change_report(change_id)
    if built is None:
        return None, ["change or its claims not found"]
    report, result = built
    if not result["passed"]:
        return None, [f"source report rejected: {i['code']}" for i in result["issues"]]

    analysis = analyze_change(change_id)
    fl = analysis["fact_layer"]
    market, comp, plan = fl["market"], fl["competitor"], fl["plan_or_sku"]
    lab = _label(fl["field_canonical"])
    new_value = _with_unit(fl["new_value"], fl["unit"])
    cited = [fl["claim_id_new"]]

    defaults = get_card_defaults(market)
    created = datetime.now(timezone.utc)
    expires = created + timedelta(days=defaults["expiry_days"])

    facts = _copy_group(report, "F", {"What changed", "Bundle value"}, lane="evidence")
    impact = _copy_group(report, "I", {"Value impact", "Ranking impact", "Bundle value"}, lane="model")

    ideas = []
    if trigger == "opportunity":
        text = (f"Idea: compare your own {lab} with {comp}'s {lab}, which is now {new_value}, "
                "and decide whether the difference is worth raising with customers who value it.")
    else:
        text = (f"Idea: check how your own {lab} and pricing compare with {comp}'s {lab}, "
                f"which is now {new_value}, and decide whether a response is needed.")
    ideas.append(_idea(1, text, cited))
    ideas.append(_idea(2, f"Idea: set a watch rule on {comp} {plan} for further changes to {lab}.", cited))

    ranking_ids = [cid for s in report["sentences"] if s["section"] == "Ranking impact"
                   for cid in s["claim_ids"]]
    if ranking_ids:
        ideas.append(_idea(
            3,
            "Idea: review the persona views where the ranking changed, because the shift may change "
            "which plans customers with those needs compare.",
            ranking_ids,
        ))

    kind = "Opportunity" if trigger == "opportunity" else "Threat"
    card = {
        "card_type": trigger, "change_id": change_id, "market": market,
        "competitor": comp, "plan_or_sku": plan, "severity": fl["severity"],
        "title": f"{kind}: {comp} {plan}, {lab} change",
        "created_at": created.isoformat(), "expires_at": expires.isoformat(),
        "owner": defaults["owner"], "status": "pending",
        "origins": report["origins"], "disclosure": report["disclosure"],
        "report_id": report["report_id"], "lane_note": LANE_NOTE, "approval_note": APPROVAL_NOTE,
        "facts": facts, "impact": impact, "ideas": ideas,
    }
    return card, []


def render_card(card: dict) -> str:
    lines = [
        f"\nCARD {card.get('card_id', '(unsaved)')}  [{card['card_type']} | {card['status']} | "
        f"owner: {card['owner']} | expires {card['expires_at']}]",
        card["title"],
    ]
    if card.get("disclosure"):
        lines.append("NOTE: " + card["disclosure"])
    lines += [card["lane_note"], card["approval_note"]]
    for group, heading in (("facts", "Facts (evidence lane)"),
                           ("impact", "Impact (model lane, calculated from the facts)"),
                           ("ideas", "Ideas (suggestions, not facts)")):
        lines.append(f"\n-- {heading} --")
        for s in card[group]:
            lines.append(f"{s['id']} {s['text']}")
            lines.append("      cites: " + ", ".join(s["claim_ids"]))
    return "\n".join(lines)


def _build_and_save(change_id: str, trigger: str, show: bool):
    card, problems = build_card(change_id, trigger)
    if card is None:
        print(f"[REJECTED] {change_id}: {'; '.join(problems)}")
        return
    result = check_card(card)
    if not result["passed"]:
        print(f"[REJECTED] {change_id}:")
        for i in result["issues"]:
            print(f"    {i['sentence_id']} {i['code']}: {i['message']}")
        return
    card_id = save_card(change_id, card, card["owner"], card["expires_at"], "pending")
    attach_card(change_id, card_id)
    print(f"[SAVED] {card_id} for {change_id} ({trigger}, owner: {card['owner']}): "
          f"facts {len(card['facts'])}, impact {len(card['impact'])}, ideas {len(card['ideas'])}")
    if show:
        print(render_card({**card, "card_id": card_id}))


def _list_cards():
    rows = get_cards(limit=1000)
    if not rows:
        print("No cards yet.")
    for row in rows:
        print(f"{row['id']}  {row['status']:<9}  owner: {row['owner']:<17}  "
              f"expires {row['expires_at']}  change: {row['change_id']}")
        print(f"    {row['body'].get('title', '')}")


def _backdate(card_id: str):
    row = get_card(card_id)
    if not row:
        print(f"Card {card_id} not found.")
        return
    created = datetime.fromisoformat(row["body"]["created_at"])
    new_expiry = (created + timedelta(seconds=1)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE cards SET expires_at = ? WHERE id = ?", (new_expiry, card_id))
    conn.commit()
    conn.close()
    print(f"TEST HOOK: {card_id} now expires at {new_expiry} (already in the past).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Person 3 Counter-Move card generator")
    parser.add_argument("--change", help="build (or show) the card for one change ID")
    parser.add_argument("--show", action="store_true", help="print full cards")
    parser.add_argument("--list", action="store_true", help="list stored cards")
    parser.add_argument("--approve", metavar="CARD_ID")
    parser.add_argument("--dismiss", metavar="CARD_ID")
    parser.add_argument("--backdate", metavar="CARD_ID", help="test hook: make a card already expired")
    args = parser.parse_args()

    expired = expire_stale_cards()
    if expired:
        print(f"Marked {expired} stale pending card(s) as expired.")

    if args.approve or args.dismiss:
        card_id, status = (args.approve, "approved") if args.approve else (args.dismiss, "dismissed")
        print(f"{card_id} -> {status}" if update_card_status(card_id, status) else f"Card {card_id} not found.")
    elif args.backdate:
        _backdate(args.backdate)
    elif args.list:
        _list_cards()
    elif args.change:
        existing = get_cards(change_id=args.change)
        if existing:
            print(f"Card already exists for {args.change}.")
            if args.show:
                print(render_card(card_view(existing[0])))
        else:
            analysis = get_analysis(args.change)
            trigger = analysis.get("card_trigger") if analysis else None
            if not trigger:
                print(f"{args.change} has no card trigger (not analysed, or not high severity with a clear direction).")
            else:
                _build_and_save(args.change, trigger, args.show)
    else:
        pending = get_pending_card_triggers()
        if not pending:
            print("No analysed changes need a card.")
        for t in pending:
            _build_and_save(t["change_id"], t["card_trigger"], args.show)