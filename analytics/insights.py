"""
insights.py
-----------
Stage 6c (historical analysis) and Stage 7 (competitor response modeling).
Model lane only. Every number comes from verified claims; thin history is
reported as "insufficient evidence", never guessed.

6c  history_for_plan / market_history
      price trend, change frequency, promo cadence, temporary-drop count
7   response_model
      leader / follower / simultaneous / insufficient evidence per competitor,
      with event counts and confidence always shown.

Run from the project root:
    python -m analytics.insights
    python -m analytics.insights --market isp
    python -m analytics.insights --selftest-only
"""

import argparse
import statistics
import sys
from datetime import datetime, timezone

from analytics.claim_store import get_claim_history, get_profiles, get_recent_changes, parse_number
from analytics.config_loader import supported_markets

EPS = 1e-9
HISTORY_DAYS = 90
MIN_PRICE_LEVELS = 3
MIN_PROMO_DAYS = 3
MIN_PAIR_EVENTS = 3
SIMULTANEOUS_HOURS = 6.0
RESPONSE_WINDOW_DAYS = 14


def _dt(ts) -> datetime:
    d = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


# ── 6c: history ──────────────────────────────────────────────────────────────

def _levels(history: list[dict]) -> list[dict]:
    levels = []
    for h in history:
        v = parse_number(h["value"])
        if v is None:
            continue
        if levels and abs(levels[-1]["value"] - v) < EPS:
            continue
        levels.append({"value": v, "claim_id": h["claim_id"],
                       "observed_at": h["observed_at"], "origin": h.get("origin")})
    return levels


def summarize_levels(levels: list[dict]) -> dict:
    n = len(levels)
    steps = [(b["value"] - a["value"]) / a["value"] * 100.0
             for a, b in zip(levels, levels[1:]) if a["value"] > EPS]
    temp_drops = sum(
        1 for i in range(1, n - 1)
        if abs(levels[i - 1]["value"] - levels[i + 1]["value"]) < EPS
        and levels[i]["value"] < levels[i - 1]["value"] - EPS)
    net = None
    trend = "no price move observed"
    if n >= 2 and levels[0]["value"] > EPS:
        net = round((levels[-1]["value"] - levels[0]["value"]) / levels[0]["value"] * 100.0, 2)
        trend = "rising" if net > 0 else "falling" if net < 0 else "flat"
    return {
        "n_levels": n,
        "first_price": levels[0]["value"] if n else None,
        "last_price": levels[-1]["value"] if n else None,
        "net_change_pct": net,
        "trend": trend,
        "largest_drop_pct": round(min(steps), 2) if steps and min(steps) < 0 else None,
        "largest_rise_pct": round(max(steps), 2) if steps and max(steps) > 0 else None,
        "temporary_drops": temp_drops,
        "evidence_level": "sufficient" if n >= MIN_PRICE_LEVELS else "insufficient evidence",
    }


def history_for_plan(market: str, competitor: str, plan: str, days: int = HISTORY_DAYS) -> dict:
    hist = get_claim_history(market, competitor, "price", plan_or_sku=plan)
    levels = _levels(hist)
    price = summarize_levels(levels)

    changes = [c for c in get_recent_changes(hours=24 * days, market=market)
               if c["competitor"] == competitor and c["plan_or_sku"] == plan]
    by_field: dict[str, int] = {}
    for c in changes:
        by_field[c["field_canonical"]] = by_field.get(c["field_canonical"], 0) + 1
    times = sorted(_dt(c["new_observed_at"]) for c in changes)
    gaps = [(b - a).total_seconds() / 86400.0 for a, b in zip(times, times[1:])]

    promo = [h for h in hist if h.get("ambiguity") == "promotional"]
    pdays = sorted({str(h["observed_at"])[:10] for h in promo})
    pdates = [datetime.fromisoformat(d) for d in pdays]
    pgaps = [(b - a).days for a, b in zip(pdates, pdates[1:])]

    claim_ids = list(dict.fromkeys(
        [lv["claim_id"] for lv in levels]
        + [c["claim_id_new"] for c in changes if c.get("claim_id_new")]
        + [h["claim_id"] for h in promo]))

    return {
        "market": market, "competitor": competitor, "plan_or_sku": plan,
        "price": price,
        "change_frequency": {
            "window_days": days, "total_changes": len(changes), "by_field": by_field,
            "avg_days_between_changes": round(statistics.mean(gaps), 2) if gaps else None,
        },
        "promo_cadence": {
            "promo_days_seen": len(pdays),
            "avg_days_between_promos": round(statistics.mean(pgaps), 1)
            if len(pdays) >= MIN_PROMO_DAYS and pgaps else None,
            "evidence_level": "sufficient" if len(pdays) >= MIN_PROMO_DAYS else "insufficient evidence",
        },
        "claim_ids": claim_ids,
        "lane": "model",
    }


def market_history(market: str) -> list[dict]:
    seen, out = set(), []
    for p in get_profiles(market):
        key = (p["competitor"], p["plan_or_sku"])
        if key in seen:
            continue
        seen.add(key)
        out.append(history_for_plan(market, p["competitor"], p["plan_or_sku"]))
    return out


# ── 7: competitor response modeling ──────────────────────────────────────────

def model_events(events: list[dict], min_events: int = MIN_PAIR_EVENTS,
                 window_days: int = RESPONSE_WINDOW_DAYS) -> dict:
    """
    events: [{"competitor","field","t"(datetime),"claim_id"}, ...]
    For each pair (a, b), every event of a is matched to the nearest event of b on
    the SAME field within +/- window. Signed lags (hours, b minus a) are collected.
    A label needs at least `min_events` matched pairs, otherwise "insufficient evidence".
    """
    comps = sorted({e["competitor"] for e in events})
    by = {c: [e for e in events if e["competitor"] == c] for c in comps}
    window_h = window_days * 24.0
    leads = {c: 0 for c in comps}
    follows = {c: 0 for c in comps}
    sims = {c: 0 for c in comps}
    paired = {c: 0 for c in comps}
    pairs = []

    for i, a in enumerate(comps):
        for b in comps[i + 1:]:
            lags = []
            for ea in by[a]:
                best = None
                for eb in by[b]:
                    if eb["field"] != ea["field"]:
                        continue
                    lag = (eb["t"] - ea["t"]).total_seconds() / 3600.0
                    if abs(lag) <= window_h and (best is None or abs(lag) < abs(best)):
                        best = lag
                if best is not None:
                    lags.append(best)
            n = len(lags)
            paired[a] += n
            paired[b] += n
            if n < min_events:
                pairs.append({"a": a, "b": b, "n": n, "median_lag_hours": None,
                              "result": "insufficient evidence"})
                continue
            med = statistics.median(lags)
            if abs(med) <= SIMULTANEOUS_HOURS:
                result = "simultaneous"
                sims[a] += 1
                sims[b] += 1
            elif med > 0:
                result = f"{a} leads {b}"
                leads[a] += 1
                follows[b] += 1
            else:
                result = f"{b} leads {a}"
                leads[b] += 1
                follows[a] += 1
            pairs.append({"a": a, "b": b, "n": n, "median_lag_hours": round(med, 2), "result": result})

    out = []
    for c in comps:
        n_events = len(by[c])
        if n_events < min_events:
            label = "insufficient evidence"
        elif leads[c] > follows[c]:
            label = "leader"
        elif follows[c] > leads[c]:
            label = "follower"
        elif sims[c] > 0 and leads[c] == follows[c] == 0:
            label = "simultaneous"
        else:
            label = "insufficient evidence"
        out.append({
            "competitor": c, "event_count": n_events, "paired_observations": paired[c],
            "label": label, "confidence": round(min(1.0, paired[c] / 10.0), 2),
            "claim_ids": [e["claim_id"] for e in by[c] if e.get("claim_id")],
        })
    return {"competitors": out, "pairs": pairs, "min_events": min_events,
            "window_days": window_days, "lane": "model"}


def response_model(market: str) -> dict:
    events = []
    for c in get_recent_changes(hours=24 * 365 * 2, market=market):
        events.append({"competitor": c["competitor"], "field": c["field_canonical"],
                       "t": _dt(c["new_observed_at"]), "claim_id": c.get("claim_id_new")})
    res = model_events(events)
    res["market"] = market
    return res


# ── Self-tests (synthetic data only, so they never depend on your DB) ────────

def run_selftests() -> bool:
    good, total = 0, 0

    def record(name, behaved, outcome):
        nonlocal good, total
        total += 1
        good += 1 if behaved else 0
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def ev(comp, day, hours=0, field="price"):
        from datetime import timedelta
        return {"competitor": comp, "field": field, "claim_id": f"{comp}-{day}-{hours}",
                "t": base + timedelta(days=day, hours=hours)}

    r = model_events([ev("A", d) for d in (0, 10, 20, 30)] + [ev("B", d + 2) for d in (0, 10, 20, 30)])
    labels = {c["competitor"]: c["label"] for c in r["competitors"]}
    record("B follows A by 2 days -> A leader, B follower",
           labels == {"A": "leader", "B": "follower"}, str(labels))

    r = model_events([ev("A", d) for d in (0, 10, 20)] + [ev("B", d, 1) for d in (0, 10, 20)])
    labels = {c["competitor"]: c["label"] for c in r["competitors"]}
    record("1-hour gap -> simultaneous", labels == {"A": "simultaneous", "B": "simultaneous"}, str(labels))

    r = model_events([ev("A", 0), ev("A", 10), ev("B", 2), ev("B", 12)])
    labels = {c["competitor"]: c["label"] for c in r["competitors"]}
    record("only 2 events each -> insufficient evidence",
           set(labels.values()) == {"insufficient evidence"}, str(labels))

    r = model_events([ev("A", d) for d in (0, 10, 20)] + [ev("B", d, field="speed_mbps") for d in (0, 10, 20)])
    labels = {c["competitor"]: c["label"] for c in r["competitors"]}
    record("different fields never pair", set(labels.values()) == {"insufficient evidence"}, str(labels))

    r = model_events([ev("A", 0), ev("A", 1), ev("A", 2)])
    record("event count always shown", r["competitors"][0]["event_count"] == 3,
           str(r["competitors"][0]["event_count"]))

    lv = [{"value": 100.0, "claim_id": "x1", "observed_at": "d1"},
          {"value": 90.0, "claim_id": "x2", "observed_at": "d2"},
          {"value": 100.0, "claim_id": "x3", "observed_at": "d3"}]
    s = summarize_levels(lv)
    record("promo-style dip counted as a temporary drop",
           s["temporary_drops"] == 1 and s["evidence_level"] == "sufficient", str(s["temporary_drops"]))
    s1 = summarize_levels(lv[:1])
    record("single price level -> insufficient evidence",
           s1["evidence_level"] == "insufficient evidence" and s1["trend"] == "no price move observed",
           s1["trend"])

    print(f"Self-tests: {good}/{total} behaved as expected.")
    return good == total


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_market(market: str):
    print(f"\n===== {market.upper()}: history =====")
    for h in market_history(market):
        p, cf, pc = h["price"], h["change_frequency"], h["promo_cadence"]
        print(f"{h['competitor']} / {h['plan_or_sku']}: price {p['first_price']} -> {p['last_price']} "
              f"({p['trend']}, net {p['net_change_pct']}%, {p['n_levels']} levels) [{p['evidence_level']}]")
        print(f"   changes in {cf['window_days']}d: {cf['total_changes']} {cf['by_field']} | "
              f"avg gap: {cf['avg_days_between_changes']} d | promo days: {pc['promo_days_seen']} "
              f"[{pc['evidence_level']}] | temporary drops: {p['temporary_drops']}")
    r = response_model(market)
    print(f"----- {market.upper()}: response modeling (min {r['min_events']} matched events) -----")
    for c in r["competitors"]:
        print(f"{c['competitor']}: {c['label']} (events={c['event_count']}, "
              f"paired={c['paired_observations']}, confidence={c['confidence']})")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 6c/7 history and response modeling")
    parser.add_argument("--market")
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()
    if args.selftest_only:
        print("\n== insights self-tests ==")
        sys.exit(0 if run_selftests() else 1)
    for m in ([args.market] if args.market else supported_markets()):
        _print_market(m)
    print("\n== insights self-tests ==")
    run_selftests()