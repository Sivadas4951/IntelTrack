"""
timelines.py (sub-stage 4.3)
----------------------------
Founder dashboard item 5: per-competitor timeline, plus price leaders.

  * Timeline: every verified value of one field over time, each point with its claim ID,
    origin, and whether a detected change (or stealth change) is attached to it.
  * Price leaders: who moves first and who follows. Labelled "insufficient evidence"
    when there are too few events, never a made-up ranking.
"""

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.components import api_error, fmt_time, fmt_value, lane_caption
from dashboard.evidence import evidence_lookup

INSUFFICIENT = "insufficient evidence"


def _num(v):
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return None


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]

    hist, err = api.get("/api/history", market=market)
    if err:
        return api_error(err)
    if not hist:
        st.info("No plans with history yet. Seed or collect some data first.")
        return

    st.subheader("Competitor timeline")
    lane_caption("evidence")

    competitors = sorted({h["competitor"] for h in hist})
    c1, c2, c3 = st.columns(3)
    comp = c1.selectbox("Competitor", competitors, key="tl_comp")
    plans = sorted({h["plan_or_sku"] for h in hist if h["competitor"] == comp})
    plan = c2.selectbox("Plan", ["All plans"] + plans, key="tl_plan")
    fields = {"price"}
    for h in hist:
        if h["competitor"] == comp and (plan == "All plans" or h["plan_or_sku"] == plan):
            fields |= set(h["change_frequency"]["by_field"])
    field = c3.selectbox("Field", sorted(fields, key=lambda f: (f != "price", f)), key="tl_field")
    st.caption("Fields listed are the ones that have changed at least once, plus price.")

    claims, err = api.get("/api/timeline", market=market, competitor=comp, field=field,
                          plan_or_sku=None if plan == "All plans" else plan)
    if err:
        return api_error(err)

    changes, _ = api.get("/api/changes", hours=24 * 365, market=market)
    by_new = {c.get("claim_id_new"): c for c in (changes or []) if c.get("competitor") == comp}

    if not claims:
        st.info("No verified observations of this field for this selection.")
    else:
        rows = []
        for c in claims:
            ch = by_new.get(c["claim_id"])
            tag = ""
            if ch:
                tag = ("STEALTH · " if ch.get("is_stealth") else "") + str(ch.get("severity", "")).upper()
            rows.append({
                "t": pd.to_datetime(c["observed_at"], errors="coerce"),
                "Observed": fmt_time(c["observed_at"]), "Plan": c["plan_or_sku"],
                "Value": fmt_value(c["value"], c.get("unit")), "_num": _num(c["value"]),
                "Change": tag, "Ambiguity": c.get("ambiguity") or "",
                "Region": c.get("region"), "Origin": c.get("origin") or "",
                "Claim ID": c["claim_id"],
            })
        df = pd.DataFrame(rows)

        num_df = df.dropna(subset=["_num", "t"])
        if len(num_df) >= 2:
            chart = alt.Chart(num_df).mark_line(interpolate="step-after", point=True).encode(
                x=alt.X("t:T", title="Observed"),
                   y=alt.Y("_num:Q", title=field, scale=alt.Scale(zero=False), axis=alt.Axis(format="~g")),
                color=alt.Color("Plan:N") if plan == "All plans" else alt.value("#2e6da4"),
                tooltip=["Observed", "Plan", "Value", "Change", "Origin", "Claim ID"],
            ).properties(height=300)
            st.altair_chart(chart, use_container_width=True)
        elif len(num_df) == 1:
            st.caption("Only one observation so far, so there is no trend to draw.")
        else:
            st.caption("This field is not numeric, so it is shown as a table only.")

        st.dataframe(df.drop(columns=["t", "_num"]).iloc[::-1], use_container_width=True, hide_index=True)
        if df["Origin"].isin(["simulated", "archived"]).any():
            st.caption("Some rows are simulated or archived data (see the Origin column), not live observations.")
        evidence_lookup(api, [c["claim_id"] for c in claims], key="tl_ev")

    _price_leaders(api, market)


def _price_leaders(api, market: str):
    st.subheader("Price leaders")
    st.caption("Who moves first and who follows, from the timing of verified changes.")
    lane_caption("model")

    pl, err = api.get("/api/price-leaders", market=market)
    if err:
        return st.warning(f"Could not load price leaders: {err}")
    comps = pl.get("competitors", [])
    if not comps:
        return st.info("No change events yet.")

    st.dataframe(pd.DataFrame([{
        "Competitor": c["competitor"], "Role": c["label"], "Events": c["event_count"],
        "Paired observations": c["paired_observations"], "Confidence": c["confidence"],
        "Claim IDs": ", ".join(c["claim_ids"]),
    } for c in comps]), use_container_width=True, hide_index=True)

    if all(c["label"] == INSUFFICIENT for c in comps):
        st.info(f"Every competitor is at '{INSUFFICIENT}' (needs at least {pl.get('min_events')} events "
                f"in {pl.get('window_days')} days). No leader is named.")

    pairs = pl.get("pairs", [])
    if pairs:
        with st.expander("Pairwise lead and lag"):
            st.dataframe(pd.DataFrame([{
                "A": p["a"], "B": p["b"], "Paired events": p["n"],
                "Median lag (hours)": p["median_lag_hours"], "Result": p["result"],
            } for p in pairs]), use_container_width=True, hide_index=True)
    evidence_lookup(api, [i for c in comps for i in c["claim_ids"]], key="pl_ev",
                    label="Evidence behind these labels")