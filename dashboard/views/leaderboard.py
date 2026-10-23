"""
leaderboard.py (sub-stage 4.2)
------------------------------
Founder dashboard item 3: effective-cost leaderboard for a chosen workload.

  * Persona picker (workload assumptions are labelled as assumptions, not facts)
  * Ranked table with the claim IDs behind every number
  * Items that fail the persona, or lack a needed fact ("not found"), are listed
    separately with the reason. Nothing is ranked on guessed data.
"""

import pandas as pd
import streamlit as st

from dashboard.components import api_error, fmt_value, lane_caption, origin_badge

METRIC_HELP = {
    "effective_monthly_cost": "Monthly price after GST handling",
    "cost_per_mbps": "₹ per Mbps per month",
    "cost_per_gb_pre_fup": "₹ per GB of fast data",
    "cost_per_litre": "₹ per litre",
    "cost_per_battery_hour": "₹ per hour of battery",
    "warranty_adjusted_cost": "Price spread over warranty months",
}


def _spend_text(v) -> str:
    if v is None or v == "":
        return "-"
    if isinstance(v, (int, float)):
        return f"₹{v:,.0f}"
    return str(v)


def _facts_text(facts: dict) -> str:
    return " · ".join(f"{k}: {v}" for k, v in (facts or {}).items() if k not in ("plan_name", "product_name", "model_name"))


def _table(entries: list[dict], unit: str) -> pd.DataFrame:
    rows = []
    for e in entries:
        rows.append({
            "Rank": e.get("rank"),
            "Competitor": e["competitor"],
            "Plan / SKU": e["plan_or_sku"],
            "Metric": e["value"],
            "Unit": unit,
            "Persona monthly spend": _spend_text(e.get("persona_monthly_spend")),
            "Key facts": _facts_text(e.get("facts_summary")),
            "Data origin": ", ".join(e.get("origins") or []) or "-",
            "Evidence claim IDs": ", ".join(e.get("evidence_claim_ids") or []),
        })
    return pd.DataFrame(rows)


def render(ctx: dict):
    api, market, region = ctx["api"], ctx["market"], ctx["region"]
    personas = ctx.get("personas") or []
    metrics = ctx.get("metrics") or []

    st.subheader("Effective-cost leaderboard")
    st.caption("Ranks what a customer really pays for what they get, not the sticker price.")

    c1, c2, c3 = st.columns([3, 2, 2])
    labels = {"": "Market-wide (no workload)"} | {p["id"]: p["label"] for p in personas}
    persona_id = c1.selectbox("Workload / persona", list(labels), format_func=lambda k: labels[k])
    rank_by = None
    if metrics:
        options = ["(persona / market default)"] + metrics
        pick = c2.selectbox("Rank by", options, format_func=lambda m: METRIC_HELP.get(m, m) if m in METRIC_HELP else m)
        rank_by = None if pick == options[0] else pick
    headline = c3.toggle("Headline claims only", value=True,
                         help="Excludes low-confidence and ambiguous claims (promotional, conditional, regional).")

    chosen = next((p for p in personas if p["id"] == persona_id), None)
    if chosen and chosen.get("assumption"):
        st.info(f"Assumption (not an observed fact): {chosen['assumption']}")

    res, err = api.get(
        "/api/leaderboard", market=market, persona_id=persona_id or None, rank_by=rank_by,
        region=None if region == "All" else region, headline_only=headline,
    )
    if err:
        return api_error(err)

    st.markdown(f"**{res['label']}** (lower is better)")
    lane_caption("model")

    for w in res.get("warnings", []):
        st.warning(w["message"])

    ranked = res.get("ranked", [])
    if ranked:
        df = _table(ranked, res.get("unit", ""))
        st.dataframe(df, use_container_width=True, hide_index=True)
        chart = pd.DataFrame(
            {"value": [e["value"] for e in ranked]},
            index=[f"{e['competitor']} · {e['plan_or_sku']}" for e in ranked],
        )
        st.bar_chart(chart)

        best = ranked[0]
        st.success(
            f"Lowest {res['label'].lower()}: {best['competitor']} · {best['plan_or_sku']} "
            f"at {fmt_value(best['value'], res.get('unit'))}"
        )
    else:
        st.info("Nothing could be ranked yet. Capture at least two plans with complete, verified facts.")

    # Items left out are shown with the reason, never silently dropped.
    for key, title in (("ineligible", "Does not meet the workload"), ("unrankable", "Cannot be ranked (a needed fact is not found)")):
        items = res.get(key, [])
        if items:
            with st.expander(f"{title} ({len(items)})"):
                for e in items:
                    st.markdown(
                        f"**{e['competitor']} · {e['plan_or_sku']}** — "
                        + "; ".join(e.get("reasons") or ["no reason recorded"])
                        + (f"  \nEvidence: {', '.join(e['evidence_claim_ids'])}" if e.get("evidence_claim_ids") else "")
                    )

    if ranked:
        with st.expander("All computed metrics per plan"):
            rows = []
            for e in ranked + res.get("ineligible", []) + res.get("unrankable", []):
                rows.append({"Competitor": e["competitor"], "Plan / SKU": e["plan_or_sku"], **(e.get("all_metrics") or {})})
            st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)
        origins = {o for e in ranked for o in (e.get("origins") or [])}
        if origins:
            st.markdown("Data origins in this ranking: " + " ".join(origin_badge(o) for o in sorted(origins)),
                        unsafe_allow_html=True)