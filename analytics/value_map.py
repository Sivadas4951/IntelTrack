"""
value_map.py
------------
Market-wide value map (model lane): weighted feature score vs price, with a
fair-value line.

How the score is built (all plain code, no LLM):
  1. Take the latest headline profile of every plan (same claim rules as the leaderboard).
  2. Features come from config/value_rules.yaml:
       quantity group : numeric, higher-is-better fields (speed, FUP cap, pack size, battery ...)
       bundle group   : true-is-better flags (ANC) and list fields (OTT bundle, counted by items)
  3. Each feature is min-max scaled across the plans being compared (0 = worst seen, 1 = best seen).
     A feature that is identical everywhere carries no information and is skipped (and reported).
  4. feature_score = 100 * weighted mean of group scores. Group weights are the
     quantity / bundle weights from value_rules.yaml, renormalised over the groups that vary.
  5. Fair-value line = ordinary least squares of price on feature_score across the plans
     (needs 3+ plans). A plan far above the line charges more than its features explain.

Honest limits, returned in `warnings`: the line only describes THIS sample of plans; scores are
relative to the plans compared, not absolute quality; a plan missing any feature fact is left
out and listed under `excluded` (never guessed).
Every point carries the claim IDs of its price and all feature inputs.
"""

from analytics.claim_store import get_profiles
from analytics.config_loader import get_market_rules

ON_LINE_BAND_PCT = 5.0
MIN_POINTS_FOR_LINE = 3


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _feature_defs(rules: dict) -> tuple[list[str], list[str]]:
    qty, bundle = [], []
    for field, ftype in rules.get("field_types", {}).items():
        if field == "price":
            continue
        direction = rules.get("field_direction", {}).get(field)
        if ftype == "number" and direction == "higher_is_better":
            qty.append(field)
        elif direction in ("true_is_better", "more_is_better"):
            bundle.append(field)
    return qty, bundle


def _raw_feature(profile: dict, field: str, ftype: str):
    fact = profile.get("facts", {}).get(field)
    if fact is None:
        return None
    v = fact["value"]
    if ftype == "number":
        return float(v) if _is_num(v) else None
    if ftype == "bool":
        return (1.0 if v else 0.0) if isinstance(v, bool) else None
    if ftype == "list":
        return float(len(v)) if isinstance(v, (list, tuple)) else None
    return None


def _ols(xs: list[float], ys: list[float]):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    intercept = my - slope * mx
    ss_tot = sum((y - my) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1 - ss_res / ss_tot if ss_tot > 0 else None
    return slope, intercept, r2


def build_value_map(market: str, region: str | None = None) -> dict:
    rules = get_market_rules(market)
    qty, bundle = _feature_defs(rules)
    ftypes = rules.get("field_types", {})
    all_features = qty + bundle

    result = {
        "market": market, "region": region, "lane": "model",
        "points": [], "excluded": [], "line": None, "features": {}, "skipped_features": [],
        "weights": {}, "warnings": [], "n_points": 0,
        "method": ("Feature score = weighted mean of min-max scaled quantity and bundle features "
                   "across the plans compared. Fair-value line = least-squares fit of price on score."),
    }

    profiles = get_profiles(market, region=region, headline_only=True)
    complete = []
    for p in profiles:
        name = f"{p['competitor']} · {p['plan_or_sku']}"
        price = p["facts"].get("price", {}).get("value")
        missing = []
        if not _is_num(price):
            missing.append("price")
        raw = {}
        for f in all_features:
            v = _raw_feature(p, f, ftypes.get(f, "number"))
            if v is None:
                missing.append(f)
            raw[f] = v
        if missing:
            result["excluded"].append({
                "name": name, "competitor": p["competitor"], "plan_or_sku": p["plan_or_sku"],
                "reason": f"not found: {', '.join(missing)}",
                "claim_ids": p["claim_ids"],
            })
        else:
            complete.append((p, name, float(price), raw))

    if len(complete) < 2:
        result["warnings"].append("Fewer than 2 plans have every needed fact, so no value map can be drawn.")
        return result

    # scale each feature across the compared plans
    scaled = {}
    for f in all_features:
        vals = [c[3][f] for c in complete]
        lo, hi = min(vals), max(vals)
        if hi > lo:
            scaled[f] = {"lo": lo, "hi": hi}
        else:
            result["skipped_features"].append(f)

    groups = {}
    for gname, feats in (("quantity", qty), ("bundle", bundle)):
        used = [f for f in feats if f in scaled]
        if used:
            groups[gname] = used
            for f in used:
                result["features"][f] = gname
    if not groups:
        result["warnings"].append("All compared plans have identical features, so there is nothing to score.")
        return result

    cfg = rules.get("weights", {})
    gw = {g: float(cfg.get(g, 0) or 0) for g in groups}
    if sum(gw.values()) <= 0:
        gw = {g: 1.0 for g in groups}
    total_w = sum(gw.values())
    result["weights"] = {g: round(w / total_w, 4) for g, w in gw.items()}

    points = []
    for p, name, price, raw in complete:
        group_scores = {}
        for g, feats in groups.items():
            norm = [(raw[f] - scaled[f]["lo"]) / (scaled[f]["hi"] - scaled[f]["lo"]) for f in feats]
            group_scores[g] = sum(norm) / len(norm)
        score = 100.0 * sum(gw[g] * s for g, s in group_scores.items()) / total_w
        evidence = [p["facts"]["price"]["claim_id"]] + [
            p["facts"][f]["claim_id"] for f in all_features if f in p["facts"]]
        origins = sorted({fact.get("origin") for fact in p["facts"].values() if fact.get("origin")})
        points.append({
            "name": name, "competitor": p["competitor"], "plan_or_sku": p["plan_or_sku"],
            "price": price, "feature_score": round(score, 2),
            "group_scores": {g: round(s * 100, 1) for g, s in group_scores.items()},
            "origins": origins,
            "evidence_claim_ids": list(dict.fromkeys(evidence)),
            "fair_price": None, "gap_pct": None, "position": "no_line",
        })

    fit = None
    if len(points) >= MIN_POINTS_FOR_LINE:
        fit = _ols([pt["feature_score"] for pt in points], [pt["price"] for pt in points])
    if fit:
        slope, intercept, r2 = fit
        result["line"] = {"slope": round(slope, 4), "intercept": round(intercept, 4),
                          "r_squared": None if r2 is None else round(r2, 4)}
        for pt in points:
            fair = intercept + slope * pt["feature_score"]
            pt["fair_price"] = round(fair, 2)
            if fair > 0:
                gap = (pt["price"] - fair) / fair * 100.0
                pt["gap_pct"] = round(gap, 1)
                pt["position"] = ("above" if gap > ON_LINE_BAND_PCT
                                  else "below" if gap < -ON_LINE_BAND_PCT else "on_line")
    else:
        result["warnings"].append(
            f"Only {len(points)} plan(s) compared; the fair-value line needs at least {MIN_POINTS_FOR_LINE}.")

    if result["line"] and result["line"]["slope"] <= 0:
        result["warnings"].append(
            "The fitted line slopes downward (more features, lower price) in this sample, "
            "so the gap figures are not meaningful.")
    result["warnings"].append(
        "The line describes only the plans shown. Scores are relative to this set, not absolute quality.")

    result["points"] = sorted(points, key=lambda x: x["feature_score"])
    result["n_points"] = len(points)
    return result