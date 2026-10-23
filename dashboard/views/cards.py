"""
cards.py (sub-stage 4.4)
------------------------
Counter-Move cards. Nothing is sent automatically: the founder approves or dismisses.

Each card shows three groups, kept visibly apart:
  Facts   evidence lane, every sentence carries claim IDs
  Impact  model lane, calculated from those facts
  Ideas   suggestions, labelled as ideas and never as facts
"""

import streamlit as st

from dashboard.components import api_error, badge, fmt_time, origin_badge, severity_badge
from dashboard.evidence import claim_ids_caption, evidence_lookup

STATUS_COLOR = {"pending": "#b9770e", "approved": "#1e8449", "dismissed": "#6b7280", "expired": "#8e44ad"}
GROUPS = (
    ("facts", "Facts", "Evidence lane: verified observations."),
    ("impact", "Impact", "Model lane: calculated from the facts above."),
    ("ideas", "Ideas", "Suggestions to consider, not facts."),
)


def _sentences(api, card_id: str, key: str, items: list[dict]):
    if not items:
        st.caption("None for this card.")
        return
    for s in items:
        st.markdown(f"- {s.get('text', '')}")
        origins = sorted({c.get("origin") for c in s.get("citations", []) if c.get("origin")})
        claim_ids_caption(s.get("claim_ids", []), prefix=f"evidence ({', '.join(origins)})" if origins else "evidence")
    evidence_lookup(api, [i for s in items for i in s.get("claim_ids", [])], key=f"{card_id}_{key}")


def _card(api, c: dict):
    b = c.get("body", {})
    status = c.get("status", "pending")
    with st.container(border=True):
        st.markdown(
            f"{severity_badge(b.get('severity'))}{badge(status.upper(), STATUS_COLOR.get(status, '#6b7280'))}"
            f"{badge(str(b.get('card_type', '')).upper(), '#2e6da4')} "
            f"**{b.get('title', 'Untitled card')}**",
            unsafe_allow_html=True,
        )
        st.caption(f"Owner: {c.get('owner')} · created {fmt_time(b.get('created_at'))} · "
                   f"expires {fmt_time(c.get('expires_at'))} · card {c['id']}")
        if b.get("disclosure"):
            st.warning(b["disclosure"])
        if b.get("origins"):
            st.markdown("".join(origin_badge(o) for o in b["origins"]), unsafe_allow_html=True)

        for key, heading, note in GROUPS:
            st.markdown(f"**{heading}**")
            st.caption(note)
            _sentences(api, c["id"], key, b.get(key, []))

        st.caption(b.get("approval_note", "Nothing is sent automatically."))
        if status == "pending":
            left, right, _ = st.columns([1, 1, 4])
            if left.button("Approve", key=f"ap_{c['id']}", type="primary"):
                _, err = api.post(f"/api/cards/{c['id']}/approve")
                st.error(err) if err else st.rerun()
            if right.button("Dismiss", key=f"di_{c['id']}"):
                _, err = api.post(f"/api/cards/{c['id']}/dismiss")
                st.error(err) if err else st.rerun()
        elif status == "expired":
            st.caption("Expired: the window to act on this has passed.")


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]

    st.subheader("Counter-Moves")
    st.caption("Cards are drafted for high-severity changes. You decide; the system sends nothing on its own.")

    status = st.radio("Status", ["pending", "approved", "dismissed", "expired", "all"],
                      horizontal=True, key="cards_status")
    cards, err = api.get("/api/cards", status=None if status == "all" else status, limit=200)
    if err:
        return api_error(err)

    cards = [c for c in cards if c.get("body", {}).get("market") == market]
    k1, k2 = st.columns(2)
    k1.metric(f"{status.capitalize()} cards", len(cards))
    k2.metric("Past expiry", sum(1 for c in cards if c.get("status") == "expired"))

    if not cards:
        st.info("No cards here. Cards appear after a high-severity change is analysed "
                "(use Re-run analysis in the sidebar).")
        return
    for c in cards:
        _card(api, c)