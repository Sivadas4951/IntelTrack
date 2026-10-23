"""
value_engine.py
---------------
Value Integrity Engine core.

  Fact layer      what changed (verified field-level diff with claim IDs)
  Value layer     what the customer gains or loses (effective price, Rs/unit, stealth)
  Strategy layer  structured impact summary (no prose here; sentences come in Stages 4/5)

Rules kept from the workflow
  * No invented values: a metric with a missing input is skipped ("not found").
  * Lists that lose items and booleans that flip to false are reported as
    qualitative losses, never given a rupee value (bundle pricing is Stage 6).
  * analyze_change() only reads. Metrics are written by recompute_market()/backfill_history().

Run a demo (from project root, after seeding):
    python -m analytics.value_engine
"""

import copy
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from analytics.config_loader import (
    canonical_field,
    get_global,
    get_market_rules,
    supported_markets,
)
from analytics.claim_store import (
    get_change,
    get_metric_history,
    get_profiles,
    get_recent_changes,
    make_metric_name,
    now_iso,
    parse_fact,
    save_metric,
)
from analytics.value_functions import (
    METRIC_LABELS,
    METRIC_SPECS,
    METRIC_UNITS,
    compute_metric,
)

EPS = 1e-9


# ── Small helpers ────────────────────────────────────────────────────────────

def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _just_before(ts: str) -> str:
    return (_parse_iso(ts) - timedelta(microseconds=1)).isoformat()


def _stealth_threshold_pct() -> float:
    return float(get_global("stealth", {}).get("min_effective_change_pct", 1.0))


def _pct(before, after):
    if not _is_num(before) or not _is_num(after) or abs(before) < EPS:
        return None
    return (after - before) / before * 100.0


def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def _find_profile(profiles: list[dict], plan: str):
    for p in profiles:
        if p["plan_or_sku"] == plan:
            return p
    return None


# ── Metrics for one profile ─────────────────────────────────────────────────

def compute_profile_metrics(profile: dict) -> dict:
    """
    {metric: {"value": float, "unit": str, "inputs": [claim_ids], "ambiguous_inputs": [fields]}}
    Metrics whose inputs are missing are left out.
    """
    market = profile["market"]
    rules = get_market_rules(market)
    values = {k: f["value"] for k, f in profile["facts"].items()}

    out = {}
    for metric in rules.get("metrics", []):
        value = compute_metric(market, metric, values, rules)
        if value is None:
            continue
        needed = METRIC_SPECS[market][metric]
        facts = profile["facts"]
        out[metric] = {
            "value": value,
            "unit": METRIC_UNITS[metric],
            "inputs": [facts[f]["claim_id"] for f in needed if f in facts],
            "ambiguous_inputs": [f for f in needed
                                 if f in facts and facts[f].get("ambiguity") not in (None, "none")],
        }
    return out


# ── Judging direction ────────────────────────────────────────────────────────

def _judge_cost_metric(before, after, thr_pct):
    """Cost metrics: lower is better. Changes smaller than thr_pct count as unchanged."""
    if not _is_num(before) or not _is_num(after):
        return "not_comparable", None
    pct = _pct(before, after)
    if pct is None:
        return "not_comparable", None
    if pct > thr_pct:
        return "worse", pct
    if pct < -thr_pct:
        return "better", pct
    return "unchanged", pct


def _judge_field(direction: str, before, after) -> str:
    """Field-level verdict: worse | better | unchanged | changed | not_comparable."""
    if before is None or after is None:
        return "not_comparable"

    if isinstance(before, list) or isinstance(after, list):
        b, a = list(before or []), list(after or [])
        if set(b) == set(a):
            return "unchanged"
        if len(a) < len(b):
            return "worse"
        if len(a) > len(b):
            return "better"
        return "changed"

    if isinstance(before, bool) or isinstance(after, bool):
        if before == after:
            return "unchanged"
        if direction == "true_is_better":
            return "better" if after else "worse"
        return "changed"

    if _is_num(before) and _is_num(after):
        if abs(after - before) < EPS:
            return "unchanged"
        if direction == "lower_is_better":
            return "better" if after < before else "worse"
        if direction == "higher_is_better":
            return "better" if after > before else "worse"
        return "changed"

    return "unchanged" if str(before) == str(after) else "changed"


# ── Fact + value + strategy layers ──────────────────────────────────────────

def _override_old_fact(profile: dict, change: dict) -> dict:
    """Make sure the 'before' profile holds exactly the OLD claim for the changed field."""
    base = copy.deepcopy(profile) if profile else {
        "market": change["market"], "competitor": change["competitor"],
        "plan_or_sku": change["plan_or_sku"], "region": change["region"],
        "facts": {}, "claim_ids": [],
    }
    canon = change["field_canonical"]
    if change.get("claim_id_old") and change.get("old_value") is not None:
        base["facts"][canon] = {
            "value": parse_fact(change["market"], canon, change["old_value"]),
            "raw": change["old_value"], "unit": change["unit"],
            "claim_id": change["claim_id_old"], "field": change["field"],
            "observed_at": change.get("old_observed_at"), "source_url": change["source_url"],
            "confidence": None, "ambiguity": "none", "origin": None, "snapshot_id": None,
        }
    elif change.get("claim_id_old") is None:
        base["facts"].pop(canon, None)  # a newly added fact had no 'before'
    return base


def analyze_change(change_id: str) -> dict | None:
    """
    Full three-layer analysis of one detected change.
    Returns None if the change or its claims are missing (for example test rows
    written by evaluate.py) so callers can skip it safely.
    """
    change = get_change(change_id)
    if not change:
        return None

    market, competitor, plan = change["market"], change["competitor"], change["plan_or_sku"]
    rules = get_market_rules(market)
    thr = _stealth_threshold_pct()
    directions = rules.get("field_direction", {})

    after_ts = change["new_observed_at"]
    after_profile = _find_profile(get_profiles(market, competitor=competitor, as_of=after_ts), plan)
    if after_profile is None:
        return None
    before_profile = _override_old_fact(
        _find_profile(get_profiles(market, competitor=competitor, as_of=_just_before(after_ts)), plan),
        change,
    )

    # ---- Fact layer ----
    old_v = parse_fact(market, change["field_canonical"], change["old_value"])
    new_v = parse_fact(market, change["field_canonical"], change["new_value"])
    fact_layer = {
        "change_id": change_id, "market": market, "competitor": competitor,
        "plan_or_sku": plan, "region": change["region"],
        "field": change["field"], "field_canonical": change["field_canonical"],
        "change_type": change["change_type"], "severity": change["severity"],
        "claim_id_old": change["claim_id_old"], "claim_id_new": change["claim_id_new"],
        "old_value": change["old_value"], "new_value": change["new_value"],
        "unit": change["unit"],
        "delta": _r(new_v - old_v) if _is_num(old_v) and _is_num(new_v) else None,
        "delta_pct": _r(_pct(old_v, new_v), 2),
        "old_observed_at": change.get("old_observed_at"),
        "new_observed_at": after_ts, "source_url": change["source_url"],
        "detected_by_p2_as_stealth": bool(change["is_stealth"]),
    }

    # ---- Value layer: derived metrics ----
    before_m = compute_profile_metrics(before_profile)
    after_m = compute_profile_metrics(after_profile)
    metrics = []
    for name in rules.get("metrics", []):
        b, a = before_m.get(name), after_m.get(name)
        if b is None and a is None:
            continue
        verdict, pct = _judge_cost_metric(b["value"] if b else None, a["value"] if a else None, thr)
        metrics.append({
            "metric": name, "label": METRIC_LABELS[name], "unit": METRIC_UNITS[name],
            "before": _r(b["value"]) if b else None,
            "after": _r(a["value"]) if a else None,
            "delta": _r(a["value"] - b["value"]) if a and b else None,
            "delta_pct": _r(pct, 2), "verdict": verdict,
            "inputs_before": b["inputs"] if b else [],
            "inputs_after": a["inputs"] if a else [],
            "ambiguous_inputs": sorted(set((b or {}).get("ambiguous_inputs", [])
                                           + (a or {}).get("ambiguous_inputs", []))),
        })

    # ---- Value layer: field-level changes and qualitative losses ----
    field_changes, qualitative_losses = [], []
    canon_fields = set(before_profile["facts"]) | set(after_profile["facts"])
    for canon in sorted(canon_fields):
        direction = directions.get(canon)
        if direction is None:
            continue  # text fields such as names are not value-judged
        bf = before_profile["facts"].get(canon)
        af = after_profile["facts"].get(canon)
        bv, av = (bf or {}).get("value"), (af or {}).get("value")
        verdict = _judge_field(direction, bv, av)
        if verdict in ("unchanged", "not_comparable"):
            continue
        field_changes.append({
            "field": canon, "before": bv, "after": av, "verdict": verdict,
            "claim_id_before": (bf or {}).get("claim_id"),
            "claim_id_after": (af or {}).get("claim_id"),
            "delta_pct": _r(_pct(bv, av), 2) if _is_num(bv) and _is_num(av) else None,
        })
        # Never give these a rupee value here (see Stage 6 bundle decomposition)
        if isinstance(bv, list) and isinstance(av, list):
            removed = [x for x in bv if x not in av]
            if removed:
                qualitative_losses.append({
                    "field": canon, "lost": removed, "value": "unquantified value loss",
                    "claim_id_before": (bf or {}).get("claim_id"),
                    "claim_id_after": (af or {}).get("claim_id"),
                })
        elif isinstance(bv, bool) and isinstance(av, bool) and verdict == "worse":
            qualitative_losses.append({
                "field": canon, "lost": [canon], "value": "unquantified value loss",
                "claim_id_before": (bf or {}).get("claim_id"),
                "claim_id_after": (af or {}).get("claim_id"),
            })

    # ---- Stealth (value layer): price unchanged but customer got less ----
    price_row = next((fc for fc in field_changes if fc["field"] == "price"), None)
    price_changed = price_row is not None
    worsened_metrics = [m["metric"] for m in metrics if m["verdict"] == "worse"]
    worse_non_price = [fc["field"] for fc in field_changes
                       if fc["verdict"] == "worse" and fc["field"] != "price"]
    stealth_basis = sorted(set(worsened_metrics + worse_non_price
                               + [q["field"] for q in qualitative_losses]))
    value_stealth = (not price_changed) and bool(stealth_basis)

    value_layer = {
        "metrics": metrics,
        "field_changes": field_changes,
        "qualitative_losses": qualitative_losses,
        "price_changed": price_changed,
        "is_value_stealth": value_stealth,
        "stealth_basis": stealth_basis if value_stealth else [],
        "stealth_threshold_pct": thr,
        "ambiguous_inputs_present": any(m["ambiguous_inputs"] for m in metrics),
    }

    # ---- Strategy layer (structured only) ----
    any_worse = bool(worsened_metrics or worse_non_price or qualitative_losses)
    any_better = any(m["verdict"] == "better" for m in metrics) or any(
        fc["verdict"] == "better" for fc in field_changes)
    if any_worse:
        direction = "worse_for_customer"
    elif any_better:
        direction = "better_for_customer"
    elif metrics or field_changes:
        direction = "neutral"
    else:
        direction = "not_comparable"

    primary = rules.get("primary_metric")
    primary_row = next((m for m in metrics if m["metric"] == primary), None)

    evidence = [change["claim_id_old"], change["claim_id_new"]]
    for m in metrics:
        if m["verdict"] in ("worse", "better"):
            evidence += m["inputs_before"] + m["inputs_after"]
    for q in qualitative_losses:
        evidence += [q["claim_id_before"], q["claim_id_after"]]
    evidence = list(dict.fromkeys(e for e in evidence if e))

    strategy_layer = {
        "impact_direction": direction,
        "headline_metric": primary,
        "headline_metric_label": METRIC_LABELS.get(primary),
        "headline_delta_pct": primary_row["delta_pct"] if primary_row else None,
        "is_stealth": value_stealth,
        "severity": change["severity"],
        "needs_counter_move": change["severity"] == "high" and direction == "worse_for_customer",
        "evidence_claim_ids": evidence,
    }

    return {
        "fact_layer": fact_layer,
        "value_layer": value_layer,
        "strategy_layer": strategy_layer,
        "lane": "model",  # derived numbers are model-lane; claim IDs link back to evidence-lane facts
    }


# ── Writing metrics ─────────────────────────────────────────────────────────

def _save_metric_row(market, competitor, plan, metric, value, inputs, computed_at, mode):
    """mode 'live': skip if identical to the latest row. mode 'backfill': skip if that timestamp exists."""
    name = make_metric_name(metric, plan)
    history = get_metric_history(market, competitor, name, limit=2000)
    if mode == "backfill":
        if any(h["computed_at"] == computed_at for h in history):
            return None
    elif history and abs(history[-1]["value"] - value) < EPS \
            and history[-1]["inputs_claim_ids"] == inputs:
        return None
    return save_metric(market, competitor, name, value, inputs, computed_at=computed_at)


def recompute_market(
    market: str,
    region: str | None = None,
    as_of: str | None = None,
    computed_at: str | None = None,
    persist: bool = True,
    headline_only: bool = True,
    mode: str = "live",
) -> list[dict]:
    """
    Compute every configured metric for every plan in a market.
    headline_only=True excludes ambiguous/low-confidence claims (two-lane rule).
    """
    profiles = get_profiles(market, region=region, headline_only=headline_only, as_of=as_of)
    stamp = computed_at or now_iso()
    rows = []
    for profile in profiles:
        for metric, m in compute_profile_metrics(profile).items():
            saved_id = None
            if persist:
                saved_id = _save_metric_row(
                    market, profile["competitor"], profile["plan_or_sku"],
                    metric, m["value"], m["inputs"], stamp, mode,
                )
            rows.append({
                "competitor": profile["competitor"], "plan_or_sku": profile["plan_or_sku"],
                "metric": metric, "label": METRIC_LABELS[metric], "unit": m["unit"],
                "value": _r(m["value"]), "inputs_claim_ids": m["inputs"],
                "ambiguous_inputs": m["ambiguous_inputs"], "saved": saved_id is not None,
            })
    return rows


def backfill_history(market: str) -> int:
    """Compute metrics as of every distinct claim time, so history charts have a real timeline."""
    conn = sqlite3.connect(DB_PATH)
    times = [r[0] for r in conn.execute(
        "SELECT DISTINCT observed_at FROM claims WHERE market = ? AND verified = 1 ORDER BY observed_at ASC",
        (market,),
    ).fetchall()]
    conn.close()

    saved = 0
    for t in times:
        rows = recompute_market(market, as_of=t, computed_at=t, mode="backfill")
        saved += sum(1 for r in rows if r["saved"])
    return saved


# ── Demo / check ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    for mkt in supported_markets():
        n = backfill_history(mkt)
        print(f"[{mkt}] metric rows saved: {n}")

    print("\n=== Analysis of recent changes ===")
    for ch in get_recent_changes(hours=24 * 30):
        result = analyze_change(ch["change_id"])
        if result is None:
            print(f"- {ch['change_id']}: skipped (claims not found)")
            continue
        fl, vl, sl = result["fact_layer"], result["value_layer"], result["strategy_layer"]
        print(f"\n{fl['competitor']} / {fl['plan_or_sku']}: {fl['field']} "
              f"{fl['old_value']} -> {fl['new_value']}  [{fl['old_observed_at']} -> {fl['new_observed_at']}]")
        print(f"  price changed: {vl['price_changed']} | value stealth: {vl['is_value_stealth']} "
              f"{vl['stealth_basis']} | direction: {sl['impact_direction']}")
        for m in vl["metrics"]:
            print(f"  {m['label']:<28} {m['before']} -> {m['after']} {m['unit']}  "
                  f"({m['delta_pct']}%)  {m['verdict']}")
        print(f"  evidence: {sl['evidence_claim_ids']}")