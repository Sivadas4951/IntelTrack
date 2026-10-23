"""
value_functions.py
------------------
Pure math for the per-market value functions. No database access.
Every function returns None when an input is missing or a divisor is zero,
so a missing fact becomes "not found" and is never guessed.

Metric definitions
  isp         effective_monthly_cost  price (x (1+GST) only if apply_gst is true)
              cost_per_mbps           effective_monthly_cost / speed_mbps
              cost_per_gb_pre_fup     effective_monthly_cost / fup_cap_gb
  dairy       cost_per_litre          price / pack_size_ml * 1000
  headphones  cost_per_battery_hour   price / battery_hours
              warranty_adjusted_cost  price / warranty_months  (Rs per warranty month)

Stage 6b adds vis_components(): the Value Integrity Score composite.
  VIS = 100 - (w_p*P + w_q*Q + w_b*B), clamped at 0.
  P = unexplained effective-price increase (0-100), from the raw price field.
  Q = reduction in included quantity/quality (0-100): the worst-hit numeric,
      higher-is-better field other than price (speed, FUP cap, pack size,
      battery hours, warranty months, ...).
  B = reduction in bundled benefits (0-100): cited-price OTT drops only, as a
      percentage of the plan's own price. An unquantified drop (no cited
      price) contributes nothing to B -- see bundle_unquantified in the result;
      callers must say so in the sentence, never silently treat it as zero loss.
  This function is pure: it takes already-parsed values, not claims, so the
  exact same code can be called from the generator (vis_score.py, fed from
  full DB profiles) and from the checker (fact_checks.py, fed from only the
  claims a sentence actually cites) and get identical numbers.
"""

METRIC_UNITS = {
    "effective_monthly_cost": "Rs/month",
    "cost_per_mbps": "Rs/Mbps/month",
    "cost_per_gb_pre_fup": "Rs/GB",
    "cost_per_litre": "Rs/litre",
    "cost_per_battery_hour": "Rs/battery-hour",
    "warranty_adjusted_cost": "Rs/warranty-month",
}

METRIC_LABELS = {
    "effective_monthly_cost": "Effective monthly cost",
    "cost_per_mbps": "Cost per Mbps",
    "cost_per_gb_pre_fup": "Cost per GB (before FUP)",
    "cost_per_litre": "Cost per litre",
    "cost_per_battery_hour": "Cost per battery hour",
    "warranty_adjusted_cost": "Warranty-adjusted cost",
}

# Canonical input fields each metric needs (names come from value_rules.yaml field_map)
METRIC_SPECS = {
    "isp": {
        "effective_monthly_cost": ["price"],
        "cost_per_mbps": ["price", "speed_mbps"],
        "cost_per_gb_pre_fup": ["price", "fup_cap_gb"],
    },
    "dairy": {
        "cost_per_litre": ["price", "pack_size_ml"],
    },
    "headphones": {
        "cost_per_battery_hour": ["price", "battery_hours"],
        "warranty_adjusted_cost": ["price", "warranty_months"],
    },
}


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def _div(a, b):
    if a is None or b is None or b == 0:
        return None
    return a / b


def effective_monthly_cost(price, gst_rate=0.0, apply_gst=False):
    if price is None:
        return None
    return price * (1.0 + gst_rate) if apply_gst else float(price)


def cost_per_mbps(eff_cost, speed_mbps):
    return _div(eff_cost, speed_mbps)


def cost_per_gb_pre_fup(eff_cost, fup_cap_gb):
    return _div(eff_cost, fup_cap_gb)


def cost_per_litre(price, pack_size_ml):
    ratio = _div(price, pack_size_ml)
    return None if ratio is None else ratio * 1000.0


def cost_per_battery_hour(price, battery_hours):
    return _div(price, battery_hours)


def warranty_adjusted_cost(price, warranty_months):
    return _div(price, warranty_months)


def compute_metric(market: str, metric: str, values: dict, rules: dict):
    """
    values: {canonical_field: parsed_value}   rules: the market block of value_rules.yaml
    Returns a float, or None if any input is missing.
    """
    needed = METRIC_SPECS.get(market, {}).get(metric)
    if needed is None:
        return None
    if any(values.get(f) is None for f in needed):
        return None

    gst_rate = float(rules.get("gst_rate", 0.0))
    apply_gst = bool(rules.get("apply_gst", False))

    if metric == "effective_monthly_cost":
        return effective_monthly_cost(values["price"], gst_rate, apply_gst)
    if metric == "cost_per_mbps":
        eff = effective_monthly_cost(values["price"], gst_rate, apply_gst)
        return cost_per_mbps(eff, values["speed_mbps"])
    if metric == "cost_per_gb_pre_fup":
        eff = effective_monthly_cost(values["price"], gst_rate, apply_gst)
        return cost_per_gb_pre_fup(eff, values["fup_cap_gb"])
    if metric == "cost_per_litre":
        return cost_per_litre(values["price"], values["pack_size_ml"])
    if metric == "cost_per_battery_hour":
        return cost_per_battery_hour(values["price"], values["battery_hours"])
    if metric == "warranty_adjusted_cost":
        return warranty_adjusted_cost(values["price"], values["warranty_months"])
    return None


# ── Stage 6b: Value Integrity Score ─────────────────────────────────────────

def vis_components(weights: dict, price_before, price_after,
                    quantity_moves: list, bundle_dropped: list) -> dict:
    """
    weights: {"price": w_p, "quantity": w_q, "bundle": w_b} (from value_rules.yaml)
    quantity_moves: [(field_name, before_value, after_value), ...] for every
        numeric, higher-is-better field other than price (whether or not it
        actually moved -- a None before/after just contributes nothing).
    bundle_dropped: [{"item", "status", "price_inr_per_month"}, ...] in the
        same shape as analytics.bundle_decomposition.decompose()["dropped"];
        status is "quantified" or anything else (treated as unquantified).
    Pure function, no DB access. Returns:
      {"p","q","b","vis"}              each a float rounded to 4 dp
      {"p_field","q_field"}            which field drove P / Q, or None
      {"bundle_quantified_rupees"}     sum of cited OTT prices for dropped items
      {"bundle_unquantified"}          True if any dropped item had no cited price
      {"weights"}                      the three weights actually used (floats)
    """
    w_p = float(weights.get("price", 0) or 0)
    w_q = float(weights.get("quantity", 0) or 0)
    w_b = float(weights.get("bundle", 0) or 0)

    p_val, p_field = 0.0, None
    if _is_num(price_before) and _is_num(price_after) and price_before > 0:
        pct = (price_after - price_before) / price_before * 100.0
        if pct > 0:
            p_val, p_field = min(100.0, pct), "price"

    q_val, q_field = 0.0, None
    for field, before, after in (quantity_moves or []):
        if not _is_num(before) or not _is_num(after) or before <= 0:
            continue
        pct = (before - after) / before * 100.0  # positive = reduction
        if pct > q_val:
            q_val, q_field = min(100.0, pct), field

    quantified_rupees = sum(
        x["price_inr_per_month"] for x in (bundle_dropped or [])
        if x.get("status") == "quantified" and _is_num(x.get("price_inr_per_month"))
    )
    unquantified = any(x.get("status") != "quantified" for x in (bundle_dropped or []))

    price_ref = None
    if _is_num(price_after) and price_after > 0:
        price_ref = price_after
    elif _is_num(price_before) and price_before > 0:
        price_ref = price_before

    b_val = 0.0
    if price_ref and quantified_rupees > 0:
        b_val = min(100.0, quantified_rupees / price_ref * 100.0)

    vis = max(0.0, 100.0 - (w_p * p_val + w_q * q_val + w_b * b_val))

    return {
        "p": round(p_val, 4), "q": round(q_val, 4), "b": round(b_val, 4), "vis": round(vis, 4),
        "p_field": p_field, "q_field": q_field,
        "bundle_quantified_rupees": round(quantified_rupees, 4),
        "bundle_unquantified": unquantified,
        "weights": {"price": w_p, "quantity": w_q, "bundle": w_b},
    }