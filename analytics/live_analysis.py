"""
live_analysis.py
----------------
Stage 3: event-driven analysis. Runs whenever a verified change arrives.

For each change it:
  1. runs the three-layer analysis (Stage 1)
  2. recomputes and saves the market's metrics (skips identical rows)
  3. re-ranks every persona before vs after the change (Stage 2)
  4. updates volatility metrics (honest "insufficient evidence" when history is thin)
  5. decides whether a Counter-Move card is warranted (Stage 5 creates the card)
  6. stores the full result in p3_change_analysis (our own table; the shared
     schema is untouched)

Card trigger rule (only for HIGH severity changes with a clear direction):
  threat       the competitor became MORE attractive to customers
  opportunity  the competitor became LESS attractive (for example a stealth cut)

Entry points
  handle_change_event(change_id)   one change
  handle_changes(changes)          what pipeline.py calls; never raises
  process_pending_changes()        analyses every change not yet analysed

Run from the project root:
    python -m analytics.live_analysis                 # process pending changes
    python -m analytics.live_analysis --force         # re-analyse everything
    python -m analytics.live_analysis --watch 10      # poll every 10 seconds
"""

import argparse
import json
import sqlite3
import statistics
import time
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from analytics.claim_store import (
    get_claim_history,
    get_recent_changes,
    now_iso,
    parse_number,
)
from analytics.personas import list_personas
from analytics.ranking import compare_rankings
from analytics.value_engine import _save_metric_row, analyze_change, recompute_market
from analytics.vis_score import vis_for_change

VOLATILITY_WINDOW_DAYS = 30
EPS = 1e-9


# ── Storage (our own table; additive, does not touch the shared schema) ─────

def ensure_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS p3_change_analysis (
            change_id TEXT PRIMARY KEY,
            market TEXT NOT NULL,
            competitor TEXT NOT NULL,
            plan_or_sku TEXT NOT NULL,
            analyzed_at TEXT NOT NULL,
            is_value_stealth INTEGER NOT NULL,
            impact_direction TEXT NOT NULL,
            severity TEXT NOT NULL,
            card_trigger TEXT,
            needs_card INTEGER NOT NULL,
            card_id TEXT,
            result_json TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def _store_result(result: dict):
    a = result["analysis"]
    sl = a["strategy_layer"]
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """INSERT INTO p3_change_analysis
           (change_id, market, competitor, plan_or_sku, analyzed_at, is_value_stealth,
            impact_direction, severity, card_trigger, needs_card, result_json)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(change_id) DO UPDATE SET
             analyzed_at = excluded.analyzed_at,
             is_value_stealth = excluded.is_value_stealth,
             impact_direction = excluded.impact_direction,
             severity = excluded.severity,
             card_trigger = excluded.card_trigger,
             needs_card = excluded.needs_card,
             result_json = excluded.result_json""",
        (result["change_id"], result["market"], result["competitor"], result["plan_or_sku"],
         result["analyzed_at"], 1 if a["value_layer"]["is_value_stealth"] else 0,
         sl["impact_direction"], sl["severity"], result["card_trigger"],
         1 if result["needs_card"] else 0, json.dumps(result)),
    )
    conn.commit()
    conn.close()


def get_analysis(change_id: str) -> dict | None:
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT result_json, card_id FROM p3_change_analysis WHERE change_id = ?", (change_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    data = json.loads(row["result_json"])
    data["card_id"] = row["card_id"]
    return data


def get_pending_card_triggers() -> list[dict]:
    """Analysed changes that warrant a card and do not have one yet (used by Stage 5)."""
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT change_id, market, competitor, plan_or_sku, card_trigger, severity, analyzed_at "
        "FROM p3_change_analysis WHERE needs_card = 1 AND card_id IS NULL ORDER BY analyzed_at ASC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def attach_card(change_id: str, card_id: str):
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE p3_change_analysis SET card_id = ? WHERE change_id = ?", (card_id, change_id))
    conn.commit()
    conn.close()


def _analyzed_ids() -> set[str]:
    conn = sqlite3.connect(DB_PATH)
    ids = {r[0] for r in conn.execute("SELECT change_id FROM p3_change_analysis").fetchall()}
    conn.close()
    return ids


# ── Time helpers ─────────────────────────────────────────────────────────────

def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _just_before(ts: str) -> str:
    return (_parse_iso(ts) - timedelta(microseconds=1)).isoformat()


# ── Re-ranking before vs after the change ───────────────────────────────────

def _state_text(state: dict) -> str:
    status = state.get("status")
    if status == "ranked":
        return f"#{state['rank']} ({state['value']})"
    return status or "absent"


def _rank_impact(market: str, after_ts: str) -> list[dict]:
    """Every persona (plus the market-wide view) where someone moved, entered or left the ranking."""
    before_ts = _just_before(after_ts)
    persona_ids = [p["id"] for p in list_personas(market)] + [None]
    impact = []
    for pid in persona_ids:
        try:
            res = compare_rankings(market, before_ts, after_ts, persona_id=pid)
        except Exception:
            continue  # a persona that cannot be ranked must not block the analysis
        label = res["persona"]["label"] if res.get("persona") else "Market-wide (no persona)"
        for row in res["rows"]:
            if not row["moved"]:
                continue
            impact.append({
                "persona_id": pid,
                "persona_label": label,
                "metric": res["label"],
                "competitor": row["competitor"],
                "plan_or_sku": row["plan_or_sku"],
                "before": _state_text(row["before"]),
                "after": _state_text(row["after"]),
            })
    return impact


# ── Volatility (only from verified price claims; "insufficient evidence" if thin) ──

def _price_levels(history: list[dict]) -> list[dict]:
    """Collapse repeated captures into distinct price levels (a re-capture is not a price move)."""
    levels = []
    for h in history:
        value = parse_number(h["value"])
        if value is None:
            continue
        if levels and abs(levels[-1]["value"] - value) < EPS:
            continue
        levels.append({"value": value, "claim_id": h["claim_id"], "observed_at": h["observed_at"]})
    return levels


def update_volatility(market: str, competitor: str, plan: str, persist: bool = True) -> dict:
    """
    price_change_count_30d  distinct price moves in the last 30 days
    price_volatility_pct    population std dev of the % price steps (needs 2+ steps)
    change_count_30d        all verified changes on this plan in the last 30 days
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=VOLATILITY_WINDOW_DAYS)
    levels = _price_levels(get_claim_history(market, competitor, "price", plan_or_sku=plan))

    steps = []
    for a, b in zip(levels, levels[1:]):
        if abs(a["value"]) > EPS:
            steps.append((b["value"] - a["value"]) / a["value"] * 100.0)

    window_levels = [lv for i, lv in enumerate(levels)
                     if i > 0 and _parse_iso(lv["observed_at"]) >= cutoff]
    price_volatility = round(statistics.pstdev(steps), 4) if len(steps) >= 2 else None

    related = [c for c in get_recent_changes(hours=24 * VOLATILITY_WINDOW_DAYS, market=market)
               if c["competitor"] == competitor and c["plan_or_sku"] == plan]

    info = {
        "price_levels_seen": len(levels),
        "price_change_count_30d": len(window_levels) if levels else None,
        "price_volatility_pct": price_volatility,
        "change_count_30d": len(related),
        "evidence_level": "sufficient" if len(steps) >= 2 else "insufficient evidence",
        "window_days": VOLATILITY_WINDOW_DAYS,
    }

    saved = 0
    if persist:
        stamp = now_iso()
        if levels:
            ids = [lv["claim_id"] for lv in window_levels] or [levels[-1]["claim_id"]]
            if _save_metric_row(market, competitor, plan, "price_change_count_30d",
                                float(len(window_levels)), ids, stamp, "live"):
                saved += 1
            if price_volatility is not None:
                if _save_metric_row(market, competitor, plan, "price_volatility_pct",
                                    float(price_volatility), [lv["claim_id"] for lv in levels],
                                    stamp, "live"):
                    saved += 1
        ids = [c["claim_id_new"] for c in related if c.get("claim_id_new")]
        if ids and _save_metric_row(market, competitor, plan, "change_count_30d",
                                    float(len(related)), ids, stamp, "live"):
            saved += 1
    info["metrics_saved"] = saved
    return info


# ── Card trigger ─────────────────────────────────────────────────────────────

def _card_trigger(strategy: dict) -> str | None:
    if strategy["severity"] != "high":
        return None
    direction = strategy["impact_direction"]
    if direction == "better_for_customer":
        return "threat"
    if direction == "worse_for_customer":
        return "opportunity"
    return None


# ── Main handler ─────────────────────────────────────────────────────────────

def handle_change_event(change_id: str) -> dict | None:
    """Full live analysis of one change. None if its claims cannot be found (safe to skip)."""
    ensure_tables()
    analysis = analyze_change(change_id)
    if analysis is None:
        return None

    fl, sl = analysis["fact_layer"], analysis["strategy_layer"]
    market, competitor, plan = fl["market"], fl["competitor"], fl["plan_or_sku"]

    rows = recompute_market(market, persist=True, headline_only=True, mode="live")
    saved = sum(1 for r in rows if r["saved"])

    impact = _rank_impact(market, fl["new_observed_at"])
    volatility = update_volatility(market, competitor, plan, persist=True)
    saved += volatility.pop("metrics_saved", 0)
    vis_result = None
    try:
        vis_result = vis_for_change(change_id)
        if vis_result and _save_metric_row(market, competitor, plan, "vis", vis_result["vis"],
                                           vis_result["claim_ids"], vis_result["computed_at"], "live"):
            saved += 1
    except Exception as exc:
        print(f"[P3] VIS failed for {change_id}: {exc}")

    trigger = _card_trigger(sl)
    result = {
        "change_id": change_id,
        "market": market,
        "competitor": competitor,
        "plan_or_sku": plan,
        "analysis": analysis,
        "rank_impact": impact,
        "ranking_changed": bool(impact),
        "volatility": volatility,
        "vis": vis_result,
        "metrics_saved": saved,
        "card_trigger": trigger,
        "needs_card": trigger is not None,
        "analyzed_at": now_iso(),
        "lane": "model",
    }
    _store_result(result)
    return result


def handle_changes(changes: list[dict]) -> list[dict]:
    """Called by pipeline.py with Person 2's change dicts. Never raises into the pipeline."""
    results = []
    for change in changes or []:
        change_id = change.get("id")
        if not change_id:
            continue
        try:
            res = handle_change_event(change_id)
            if res:
                results.append(res)
        except Exception as exc:
            print(f"[P3] analysis failed for {change_id}: {exc}")
    return results


def process_pending_changes(hours: int = 24 * 30, force: bool = False, market: str | None = None) -> list[dict]:
    """Analyse every change not yet analysed (oldest first). force=True re-analyses all."""
    ensure_tables()
    done = _analyzed_ids()
    results = []
    for change in reversed(get_recent_changes(hours=hours, market=market)):
        if change["change_id"] in done and not force:
            continue
        try:
            res = handle_change_event(change["change_id"])
        except Exception as exc:
            print(f"[P3] analysis failed for {change['change_id']}: {exc}")
            continue
        if res:
            results.append(res)
    return results


# ── Demo ─────────────────────────────────────────────────────────────────────

def _print_result(res: dict):
    a = res["analysis"]
    fl, vl, sl = a["fact_layer"], a["value_layer"], a["strategy_layer"]
    print(f"\n{fl['competitor']} / {fl['plan_or_sku']}: {fl['field']} {fl['old_value']} -> {fl['new_value']}")
    print(f"  severity: {sl['severity']} | direction: {sl['impact_direction']} | "
          f"value stealth: {vl['is_value_stealth']}")
    for m in vl["metrics"]:
        if m["verdict"] in ("worse", "better"):
            print(f"  {m['label']}: {m['before']} -> {m['after']} {m['unit']} ({m['delta_pct']}%) {m['verdict']}")
    if res["rank_impact"]:
        print("  ranking impact:")
        for r in res["rank_impact"]:
            print(f"    [{r['persona_label']}] {r['competitor']}: {r['before']} -> {r['after']}")
    else:
        print("  ranking impact: none")
    v = res["volatility"]
    print(f"  volatility: price moves (30d) = {v['price_change_count_30d']}, "
          f"price volatility = {v['price_volatility_pct']}, changes (30d) = {v['change_count_30d']} "
          f"[{v['evidence_level']}]")
    print(f"  metric rows saved: {res['metrics_saved']} | card: "
          f"{res['card_trigger'] if res['needs_card'] else 'not needed'}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Person 3 live analysis")
    parser.add_argument("--force", action="store_true", help="re-analyse changes already processed")
    parser.add_argument("--hours", type=int, default=24 * 30, help="look-back window for changes")
    parser.add_argument("--watch", type=int, default=0, help="poll every N seconds (Ctrl+C to stop)")
    args = parser.parse_args()

    def _run_once(force: bool):
        results = process_pending_changes(hours=args.hours, force=force)
        if not results:
            print("No new changes to analyse.")
        for r in results:
            _print_result(r)
        return results

    if args.watch > 0:
        print(f"Watching for new changes every {args.watch}s. Ctrl+C to stop.")
        try:
            first = True
            while True:
                _run_once(force=args.force and first)
                first = False
                time.sleep(args.watch)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        _run_once(force=args.force)