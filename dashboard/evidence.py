"""
evidence.py
-----------
Shared evidence helpers for the dashboard views (sub-stages 4.3 and 4.4).
Every number or sentence shown in a view links back to claim IDs; this lets the
reader open any claim and see what was observed, where and when.
"""

import streamlit as st

from dashboard.components import origin_badge, fmt_time, fmt_value

ORIGIN_NOTE = {
    "simulated": "Simulated demo data, not a live observation.",
    "archived": "Archived observation, not captured live.",
}


def claim_ids_caption(ids: list[str], prefix: str = "evidence"):
    ids = [i for i in dict.fromkeys(ids or []) if i]
    st.caption(f"{prefix}: {', '.join(ids)}" if ids else f"{prefix}: none")


def evidence_lookup(api, claim_ids: list[str], key: str, label: str = "Open evidence"):
    """Expander with a toggle: the claim is only fetched once the reader asks for it."""
    ids = [i for i in dict.fromkeys(claim_ids or []) if i]
    if not ids:
        return
    with st.expander(f"{label} ({len(ids)} claim{'s' if len(ids) != 1 else ''})"):
        if not st.toggle("Load claim details", key=f"{key}_t"):
            return
        pick = st.selectbox("Claim", ids, key=f"{key}_s")
        data, err = api.get(f"/api/claims/{pick}")
        if err:
            st.warning(err)
            return
        c, snap = data.get("claim", {}), data.get("snapshot") or {}
        origin = c.get("origin") or snap.get("origin")
        if origin:
            st.markdown(origin_badge(origin), unsafe_allow_html=True)
            if origin in ORIGIN_NOTE:
                st.caption(ORIGIN_NOTE[origin])
        st.markdown(
            f"**{c.get('competitor')}** · {c.get('plan_or_sku')} · `{c.get('field')}` = "
            f"**{fmt_value(c.get('value'), c.get('unit'))}**"
        )
        st.caption(
            f"Observed {fmt_time(c.get('observed_at'))} · region {c.get('region')} · "
            f"verified {bool(c.get('verified'))} · snapshot {c.get('snapshot_id')}"
        )
        if c.get("source_url"):
            st.markdown(f"Source: {c['source_url']}")
        if snap:
            st.json(snap, expanded=False)