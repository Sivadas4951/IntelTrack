"""
ranking.py
----------
Stage 2: ranked cost tables, rank shifts over time, and configurable comparison.

  rank_market()          ranked cost table, optionally for a persona
  compare_rankings()     rank before vs after a point in time (shows what a change did)
  compare_competitors()  side-by-side table on user-chosen criteria ("not found" if missing)

Everything is model-lane output. Each row carries claim IDs back to evidence.

Run a demo (from project root, after seeding):
    python -m analytics.ranking
"""

from datetime import datetime, timezone

from analytics.claim_store import get_profiles
from analytics.config_loader import get_market_rules, supported_markets
from analytics.personas import (
    evaluate_persona,
    get_comparability_fields,
    get_persona,
    persona_summary,
)
from analytics.value_engine import compute_profile_metrics
from analytics.value_functions import METRIC_LABELS, METRIC_SPECS, METRIC_UNITS


# ── Helpers ──────────────────────────────────────────────────────────────────

def _r(x, nd=4):
    return None if x is None else round(float(x), nd)


def _fmt(v):
    if isinstance(v, float) and v.is_integer():
        return int(v)
    return v


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _missing_inputs(market: str, metric: str, profile: dict) -> list[str]:
    needed = METRIC_SPECS[market][metric]
    facts = profile["facts"]
    return [f for f in needed if facts.get(f) is None or facts[f]["value"] is None]


def _persona_spend(persona: dict | None, metrics: dict):
    litres = ((persona or {}).get("usage") or {}).get("monthly_litres")
    cpl = metrics.get("cost_per_litre")
    if litres is None or cpl is None:
        return None
    return round(float(litres) * cpl["value"], 2)


def _origins(profile: dict) -> list[str]:
    return sorted({f.get("origin") for f in profile["facts"].values() if f.get("origin")})


# ── Ranked cost table ───────────────────────────────────────────────────────

def rank_market(
    market: str,
    persona_id: str | None = None,
    rank_by: str | None = None,
    region: str | None = None,
    headline_only: bool = True,
    as_of: str | None = None,
) -> dict:
    """
    Ranks plans by a cost metric (lower is better).
      ranked      meets the persona (or no persona) and has every input
      ineligible  fails a persona requirement, with the reason and claim ID
      unrankable  a needed fact is "not found" (never guessed)
    headline_only=True leaves out low-confidence and ambiguous claims.
    """
    rules = get_market_rules(market)
    persona = get_persona(market, persona_id)
    metric = rank_by or (persona or {}).get("rank_by") or rules.get("primary_metric")
    if metric not in rules.get("metrics", []):
        raise ValueError(
            f"'{metric}' is not a metric for '{market}'. Available: {rules.get('metrics', [])}"
        )

    profiles = get_profiles(market, region=region, headline_only=headline_only, as_of=as_of)
    ranked, ineligible, unrankable = [], [], []

    for p in profiles:
        metrics = compute_profile_metrics(p)
        mrow = metrics.get(metric)
        check = evaluate_persona(p, persona) if persona else None

        entry = {
            "competitor": p["competitor"],
            "plan_or_sku": p["plan_or_sku"],
            "region": p["region"],
            "metric": metric,
            "label": METRIC_LABELS[metric],
            "unit": METRIC_UNITS[metric],
            "value": _r(mrow["value"]) if mrow else None,
            "inputs_claim_ids": mrow["inputs"] if mrow else [],
            "ambiguous_inputs": mrow["ambiguous_inputs"] if mrow else [],
            "all_metrics": {k: _r(v["value"]) for k, v in metrics.items()},
            "facts_summary": {k: f["value"] for k, f in p["facts"].items()},
            "origins": _origins(p),
            "persona_check": check,
            "persona_monthly_spend": _persona_spend(persona, metrics),
            "reasons": [],
            "evidence_claim_ids": [],
        }

        persona_claims = [c["claim_id"] for c in (check or {}).get("checks", []) if c["claim_id"]]

        if check and check["eligible"] is False:
            failed = [c for c in check["checks"] if c["passed"] is False]
            entry["reasons"] = [
                f"{c['field']} {c['op']} {_fmt(c['required'])} not met (found {_fmt(c['actual'])})"
                for c in failed
            ]
            entry["evidence_claim_ids"] = [c["claim_id"] for c in failed]
            ineligible.append(entry)
        elif mrow is None:
            entry["reasons"] = [f"not found: {', '.join(_missing_inputs(market, metric, p))}"]
            unrankable.append(entry)
        elif check and check["eligible"] is None:
            entry["reasons"] = [
                f"cannot verify persona requirement, not found: {', '.join(check['missing'])}"
            ]
            unrankable.append(entry)
        else:
            entry["evidence_claim_ids"] = list(dict.fromkeys(entry["inputs_claim_ids"] + persona_claims))
            ranked.append(entry)

    ranked.sort(key=lambda e: (e["value"], e["competitor"], e["plan_or_sku"]))
    for e in ranked:
        e["rank"] = 1 + sum(1 for o in ranked if o["value"] < e["value"])

    # Warnings: honest limits of the ranking
    warnings = []
    for field in get_comparability_fields(market):
        seen = {str(_fmt(e["facts_summary"].get(field))) for e in ranked
                if e["facts_summary"].get(field) is not None}
        if len(seen) > 1:
            warnings.append({
                "type": "not_like_for_like", "field": field, "values": sorted(seen),
                "message": f"{field} differs across ranked items ({', '.join(sorted(seen))}); "
                           f"{METRIC_LABELS[metric]} is not strictly like-for-like.",
            })
    if any(e["ambiguous_inputs"] for e in ranked):
        warnings.append({
            "type": "ambiguity",
            "message": "Some ranked values come from ambiguous claims (for example 'up to' or a range).",
        })
    if len(ranked) < 2:
        warnings.append({
            "type": "thin_ranking",
            "message": f"Only {len(ranked)} item(s) ranked; a ranking needs at least 2 to be meaningful.",
        })

    return {
        "market": market,
        "persona": persona_summary(persona) if persona else None,
        "metric": metric,
        "label": METRIC_LABELS[metric],
        "unit": METRIC_UNITS[metric],
        "as_of": as_of,
        "headline_only": headline_only,
        "ranked": ranked,
        "ineligible": ineligible,
        "unrankable": unrankable,
        "warnings": warnings,
        "lane": "model",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _status_map(result: dict) -> dict:
    out = {}
    for e in result["ranked"]:
        out[(e["competitor"], e["plan_or_sku"])] = {
            "status": "ranked", "rank": e["rank"], "value": e["value"],
        }
    for e in result["ineligible"]:
        out[(e["competitor"], e["plan_or_sku"])] = {"status": "ineligible", "reasons": e["reasons"]}
    for e in result["unrankable"]:
        out[(e["competitor"], e["plan_or_sku"])] = {"status": "unrankable", "reasons": e["reasons"]}
    return out


def compare_rankings(
    market: str,
    before_ts: str,
    after_ts: str,
    persona_id: str | None = None,
    rank_by: str | None = None,
    region: str | None = None,
    headline_only: bool = True,
) -> dict:
    """Ranking at two points in time: shows who moved or dropped out after a change."""
    before = rank_market(market, persona_id, rank_by, region, headline_only, as_of=before_ts)
    after = rank_market(market, persona_id, rank_by, region, headline_only, as_of=after_ts)
    bm, am = _status_map(before), _status_map(after)

    rows = []
    for key in sorted(set(bm) | set(am)):
        b = bm.get(key, {"status": "absent"})
        a = am.get(key, {"status": "absent"})
        moved = b.get("status") != a.get("status") or b.get("rank") != a.get("rank")
        rows.append({
            "competitor": key[0], "plan_or_sku": key[1],
            "before": b, "after": a, "moved": moved,
        })

    return {
        "market": market,
        "persona": after["persona"],
        "metric": after["metric"],
        "label": after["label"],
        "before_ts": before_ts,
        "after_ts": after_ts,
        "rows": rows,
        "lane": "model",
    }


# ── Configurable comparison ─────────────────────────────────────────────────

def _best_keys(criterion: str, is_metric: bool, direction: str | None, row_cells: list) -> list[str]:
    """Which rows are best on one criterion. Empty if there is no clear direction."""
    usable = [(k, c["value"]) for k, c in row_cells if c["value"] is not None]
    if len(usable) < 2:
        return []

    if is_metric:
        direction = "lower_is_better"

    if direction in ("lower_is_better", "higher_is_better") and all(_is_num(v) for _, v in usable):
        target = (min if direction == "lower_is_better" else max)(v for _, v in usable)
        return [k for k, v in usable if v == target]

    if direction == "true_is_better" and all(isinstance(v, bool) for _, v in usable):
        winners = [k for k, v in usable if v is True]
        return winners if 0 < len(winners) < len(usable) else []

    if direction == "more_is_better" and all(isinstance(v, list) for _, v in usable):
        target = max(len(v) for _, v in usable)
        return [k for k, v in usable if len(v) == target]

    return []


def compare_competitors(
    market: str,
    criteria: list[str] | None = None,
    competitors: list[str] | None = None,
    region: str | None = None,
    as_of: str | None = None,
) -> dict:
    """
    Side-by-side table on user-chosen criteria. Criteria can be canonical facts
    (price, speed_mbps, ...) or metrics (cost_per_mbps, ...).
    A missing value is shown as "not found", never guessed.
    Ambiguous or low-confidence values are shown and flagged, not hidden.
    """
    rules = get_market_rules(market)
    fact_names = set(rules.get("field_map", {}).keys())
    metric_names = set(rules.get("metrics", []))
    criteria = list(criteria or rules.get("criteria_default", []))

    unknown = [c for c in criteria if c not in fact_names and c not in metric_names]
    if unknown:
        raise ValueError(
            f"Unknown criteria {unknown}. Allowed facts: {sorted(fact_names)}; "
            f"allowed metrics: {sorted(metric_names)}"
        )

    profiles = get_profiles(market, region=region, headline_only=False, as_of=as_of)
    if competitors:
        wanted = {c.lower() for c in competitors}
        profiles = [p for p in profiles if p["competitor"].lower() in wanted]

    directions = rules.get("field_direction", {})
    rows, not_found = [], 0

    for p in profiles:
        metrics = compute_profile_metrics(p)
        cells = {}
        for c in criteria:
            if c in metric_names:
                m = metrics.get(c)
                if m is None:
                    cells[c] = {"kind": "metric", "value": None, "status": "not found",
                                "claim_ids": [], "ambiguous_inputs": []}
                    not_found += 1
                    continue
                used = [f for f in p["facts"].values() if f["claim_id"] in m["inputs"]]
                cells[c] = {
                    "kind": "metric", "value": _r(m["value"]), "unit": m["unit"],
                    "status": "ambiguous" if m["ambiguous_inputs"] else "ok",
                    "claim_ids": m["inputs"], "ambiguous_inputs": m["ambiguous_inputs"],
                    "observed_at": max((f["observed_at"] for f in used), default=None),
                    "source_url": used[0]["source_url"] if used else None,
                }
            else:
                f = p["facts"].get(c)
                if f is None or f["value"] is None:
                    cells[c] = {"kind": "fact", "value": None, "status": "not found", "claim_id": None}
                    not_found += 1
                    continue
                ambiguous = f.get("ambiguity") not in (None, "none")
                cells[c] = {
                    "kind": "fact", "value": f["value"], "raw": f["raw"], "unit": f["unit"],
                    "status": "ambiguous" if ambiguous else "ok",
                    "claim_id": f["claim_id"], "source_url": f["source_url"],
                    "observed_at": f["observed_at"], "origin": f.get("origin"),
                    "confidence": f.get("confidence"), "ambiguity": f.get("ambiguity"),
                }
        rows.append({"competitor": p["competitor"], "plan_or_sku": p["plan_or_sku"], "cells": cells})

    best = {}
    for c in criteria:
        row_cells = [(f"{r['competitor']} / {r['plan_or_sku']}", r["cells"][c]) for r in rows]
        best[c] = _best_keys(c, c in metric_names, directions.get(c), row_cells)

    columns = [
        {"key": c, "kind": "metric" if c in metric_names else "fact",
         "label": METRIC_LABELS.get(c, c),
         "unit": METRIC_UNITS.get(c)}
        for c in criteria
    ]

    return {
        "market": market, "criteria": criteria, "columns": columns, "rows": rows,
        "best": best, "not_found_count": not_found, "as_of": as_of, "lane": "model",
    }


# ── Demo ────────────────────────────────────────────────────────────────────

def _print_ranking(res: dict):
    p = res["persona"]
    print(f"\n[{res['market']}] ranked by {res['label']} ({res['unit']})  "
          f"persona: {p['label'] if p else 'none'}")
    for e in res["ranked"]:
        spend = (f" | monthly spend Rs {e['persona_monthly_spend']}"
                 if e.get("persona_monthly_spend") is not None else "")
        print(f"  #{e['rank']} {e['competitor']} / {e['plan_or_sku']}: {e['value']}{spend}")
    for e in res["ineligible"]:
        print(f"  x {e['competitor']} / {e['plan_or_sku']}: ineligible - {'; '.join(e['reasons'])}")
    for e in res["unrankable"]:
        print(f"  ? {e['competitor']} / {e['plan_or_sku']}: {'; '.join(e['reasons'])}")
    for w in res["warnings"]:
        print(f"  ! {w['message']}")


def _show_state(s: dict) -> str:
    return f"#{s['rank']} ({s['value']})" if s["status"] == "ranked" else s["status"]


if __name__ == "__main__":
    DEMO_PERSONAS = {
        "isp": "heavy_streaming_household",
        "dairy": "family_daily",
        "headphones": "commuter_anc",
    }
    BEFORE_TS = "2026-10-23T10:00:00+00:00"   # before the seeded flips
    AFTER_TS = "2026-10-23T12:00:00+00:00"    # after the seeded flips

    print("=== Ranked cost tables (latest data, with persona) ===")
    for mkt in supported_markets():
        _print_ranking(rank_market(mkt, persona_id=DEMO_PERSONAS[mkt]))

    print("\n=== Dairy with no persona (note the like-for-like warning) ===")
    _print_ranking(rank_market("dairy"))

    print("\n=== What each stealth change did to the ranking ===")
    for mkt in supported_markets():
        res = compare_rankings(mkt, BEFORE_TS, AFTER_TS, persona_id=DEMO_PERSONAS[mkt])
        print(f"\n[{mkt}] {res['persona']['label']} | ranked by {res['label']}")
        for r in res["rows"]:
            mark = "  <-- changed" if r["moved"] else ""
            print(f"  {r['competitor']:<14} {_show_state(r['before']):<16} -> {_show_state(r['after'])}{mark}")

    print("\n=== Configurable comparison: ISP default criteria ===")
    table = compare_competitors("isp")
    print("criteria:", table["criteria"], "| not found cells:", table["not_found_count"])
    for r in table["rows"]:
        vals = {k: (c["value"] if c["status"] != "not found" else "not found") for k, c in r["cells"].items()}
        print(f"  {r['competitor']:<12} {vals}")
    print("  best by column:", table["best"])

    print("\n=== Configurable comparison: custom criteria (price + a metric) ===")
    table = compare_competitors("isp", criteria=["price", "cost_per_mbps"])
    for r in table["rows"]:
        print(f"  {r['competitor']:<12}",
              {k: c["value"] for k, c in r["cells"].items()})
    print("  best by column:", table["best"])