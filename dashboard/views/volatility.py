"""
volatility.py (sub-stage 4.2)
-----------------------------
Founder dashboard item 6: volatility and promo calendar per competitor.

  * Change heatmap: how often each field of each plan changed
  * Price behaviour: trend, 30-day price moves, volatility
  * Promo cadence: how often promotional pricing was seen
Where there is too little history the cell says "insufficient evidence"
rather than showing a made-up number.
"""

import pandas as pd
import streamlit as st

from dashboard.components import api_error, lane_caption

INSUFFICIENT = "insufficient evidence"


def _heat(v):
    """White to red by count. Plain CSS, no matplotlib needed."""
    try:
        n = float(v)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    alpha = min(0.15 + 0.2 * n, 0.9)
    return f"background-color: rgba(192, 57, 43, {alpha}); color: #111"


def _metric_table(rows: list[dict]) -> pd.DataFrame:
    """Pivot stored metric rows (name@plan) into one row per competitor + plan."""
    wanted = ("price_change_count_30d", "price_volatility_pct", "change_count_30d")
    table: dict[tuple, dict] = {}
    for r in rows:
        base, _, plan = str(r.get("metric_name", "")).partition("@")
        if base not in wanted:
            continue
        key = (r.get("competitor"), plan)
        table.setdefault(key, {})[base] = r.get("value")
    out = []
    for (comp, plan), vals in sorted(table.items()):
        vol = vals.get("price_volatility_pct")
        out.append({
            "Competitor": comp,
            "Plan / SKU": plan,
            "Price moves (30d)": int(vals["price_change_count_30d"]) if vals.get("price_change_count_30d") is not None else INSUFFICIENT,
            "Price volatility (std dev of % steps)": f"{vol:.2f}%" if vol is not None else INSUFFICIENT,
            "All changes (30d)": int(vals["change_count_30d"]) if vals.get("change_count_30d") is not None else 0,
        })
    return pd.DataFrame(out)


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]

    hist, err = api.get("/api/history", market=market)
    if err:
        return api_error(err)
    metrics, m_err = api.get("/api/metrics", market=market)

    st.subheader("Volatility and promo calendar")
    lane_caption("model")

    if not hist:
        st.info("No plans with history yet. Run seed_demo_claims or let the scheduler capture more snapshots.")
        return

    # ── change heatmap ──────────────────────────────────────────────────────
    st.markdown("#### Change heatmap")
    days = hist[0]["change_frequency"]["window_days"]
    st.caption(f"Number of verified changes per field in the last {days} days. Darker means more frequent.")
    fields = sorted({f for h in hist for f in h["change_frequency"]["by_field"]})
    if fields:
        rows = {}
        for h in hist:
            label = f"{h['competitor']} · {h['plan_or_sku']}"
            rows[label] = {f: h["change_frequency"]["by_field"].get(f, 0) for f in fields}
        df = pd.DataFrame.from_dict(rows, orient="index")
        st.dataframe(df.style.map(_heat), use_container_width=True)
    else:
        st.info("No field changes recorded in this window.")

    # ── price behaviour ─────────────────────────────────────────────────────
    st.markdown("#### Price behaviour")
    rows = []
    for h in hist:
        p = h["price"]
        net = p.get("net_change_pct")
        rows.append({
            "Competitor": h["competitor"],
            "Plan / SKU": h["plan_or_sku"],
            "Trend": p.get("trend", "-"),
            "Net change": f"{net:+.1f}%" if isinstance(net, (int, float)) else "-",
            "Price levels seen": p.get("n_levels", 0),
            "Evidence": p.get("evidence_level", "-"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    if m_err:
        st.warning(f"Stored volatility metrics unavailable: {m_err}")
    else:
        mt = _metric_table(metrics or [])
        if not mt.empty:
            st.markdown("##### 30-day volatility (stored metrics)")
            st.dataframe(mt, use_container_width=True, hide_index=True)
            st.caption("Volatility needs at least two price steps. Fewer than that is shown as insufficient evidence.")
        else:
            st.caption("No stored volatility metrics yet. Press 'Re-run analysis' in the sidebar.")

    # ── promo calendar ──────────────────────────────────────────────────────
    st.markdown("#### Promo cadence")
    rows = []
    for h in hist:
        pc = h["promo_cadence"]
        avg = pc.get("avg_days_between_promos")
        rows.append({
            "Competitor": h["competitor"],
            "Plan / SKU": h["plan_or_sku"],
            "Promo days seen": pc.get("promo_days_seen", 0),
            "Avg days between promos": avg if avg is not None else INSUFFICIENT,
            "Evidence": pc.get("evidence_level", "-"),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
    st.caption("A promo day is a day where a price claim was flagged promotional (sale, offer, % off).")

    with st.expander("Evidence claim IDs behind these numbers"):
        for h in hist:
            st.markdown(f"**{h['competitor']} · {h['plan_or_sku']}**: {', '.join(h.get('claim_ids') or []) or '-'}")