"""
overview.py (sub-stage 4.1)
---------------------------
Founder dashboard items 2 and the alert feed:
  * KPI row
  * Stealth-change feed (price same, customer gets less)
  * Everything that changed in the selected window, with severity and evidence IDs
  * Alert feed with useful / noise feedback (used later for alert tuning)
"""

import streamlit as st

from dashboard.components import (
    api_error, changes_to_df, fmt_time, fmt_value, lane_caption,
    severity_badge, stealth_badge,
)


def render(ctx: dict):
    api, market, region, hours = ctx["api"], ctx["market"], ctx["region"], ctx["hours"]

    changes, err = api.get("/api/changes", hours=hours, market=market)
    if err:
        return api_error(err)
    alerts, a_err = api.get("/api/alerts", hours=hours)

    if region != "All":
        changes = [c for c in changes if c.get("region") == region]
    alerts = [a for a in (alerts or []) if a.get("market") == market]
    external = [a for a in alerts if a.get("channel") != "digest"]

    stealth = [c for c in changes if c.get("is_stealth")]
    high = [c for c in changes if c.get("severity") == "high"]

    k1, k2, k3, k4 = st.columns(4)
    k1.metric(f"Changes (last {hours}h)", len(changes))
    k2.metric("Stealth changes", len(stealth))
    k3.metric("High severity", len(high))
    k4.metric("Alerts sent", len(external))

    # ── stealth feed ────────────────────────────────────────────────────────
    st.subheader("Stealth-change feed")
    st.caption("The sticker price did not move, but the customer now gets less. A plain price watcher misses these.")
    lane_caption("evidence")
    if not stealth:
        st.info("No stealth changes in this window.")
    for c in stealth:
        with st.container(border=True):
            st.markdown(
                f"{stealth_badge()}{severity_badge(c.get('severity'))} "
                f"**{c.get('competitor')}** · {c.get('plan_or_sku')} · `{c.get('field')}`",
                unsafe_allow_html=True,
            )
            st.markdown(
                f"### {fmt_value(c.get('old_value'), c.get('unit'))} → {fmt_value(c.get('new_value'), c.get('unit'))}"
            )
            st.caption(
                f"Detected {fmt_time(c.get('detected_at'))} · region {c.get('region')} · "
                f"evidence {c.get('claim_id_old') or '-'} → {c.get('claim_id_new') or '-'}"
            )

    # ── all changes ─────────────────────────────────────────────────────────
    st.subheader("All changes")
    lane_caption("evidence")
    if changes:
        st.dataframe(changes_to_df(changes), use_container_width=True, hide_index=True)
    else:
        st.info("No verified changes in this window. Flip something on the mock admin panel (port 8001/admin).")

    # ── alert feed ──────────────────────────────────────────────────────────
    st.subheader("Alert feed")
    if a_err:
        st.warning(f"Could not load alerts: {a_err}")
    elif not alerts:
        st.info("No alerts in this window.")
    for a in alerts[:10]:
        left, mid, right = st.columns([6, 1, 1])
        left.markdown(
            f"{severity_badge(a.get('severity'))}"
            f"{stealth_badge() if a.get('is_stealth') else ''} "
            f"**{a.get('competitor')}** · `{a.get('field')}` · via {a.get('channel')} · {fmt_time(a.get('sent_at'))}"
            f"{' · feedback: ' + a['feedback'] if a.get('feedback') else ''}",
            unsafe_allow_html=True,
        )
        if mid.button("Useful", key=f"u_{a['id']}"):
            api.post(f"/api/alerts/{a['id']}/feedback", json_body={"feedback": "useful"})
            st.rerun()
        if right.button("Noise", key=f"n_{a['id']}"):
            api.post(f"/api/alerts/{a['id']}/feedback", json_body={"feedback": "noise"})
            st.rerun()