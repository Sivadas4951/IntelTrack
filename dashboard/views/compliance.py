"""
compliance.py (sub-stage 5.3)
-----------------------------
Compliance panel: what is monitored, how politely, and whether the stored evidence
has been tampered with.
"""

import pandas as pd
import streamlit as st

from dashboard.components import api_error, badge, fmt_time, origin_badge

ORIGIN_MEANING = {
    "live": "Captured by our own monitor from the page.",
    "archived": "Recovered from an archive (for example the Wayback Machine). Not captured live.",
    "simulated": "Generated to fill history for the demo. Labelled everywhere it is used.",
}


def render(ctx: dict):
    api = ctx["api"]
    st.subheader("Compliance")
    data, err = api.get("/api/compliance")
    if err:
        return api_error(err)

    pol, log, sources = data["policy"], data["log"], data["sources"]

    # policy and robots
    st.markdown("#### How we fetch")
    a, b, c, d = st.columns(4)
    a.metric("Min seconds between requests (per domain)",
             pol.get("rate_limit_seconds") if pol.get("rate_limit_seconds") is not None else "n/a")
    b.metric("robots.txt checks allowed", log["robots_allowed"])
    c.metric("robots.txt checks refused", log["robots_disallowed"])
    d.metric("Rate-limit waits", log["counts"].get("RATE_LIMIT", 0))
    st.markdown(f"User agent: `{pol.get('user_agent') or 'n/a'}`")
    if pol.get("error"):
        st.warning(f"Could not read the collector settings: {pol['error']}")
    if log["blocked_urls"]:
        st.warning("Fetches refused by robots.txt (not collected):")
        for u in log["blocked_urls"]:
            st.markdown(f"- {u}")
    if not log["exists"] or log["total_entries"] == 0:
        st.info("The audit log is empty. It fills as the collector runs.")

    # sources
    st.markdown("#### Sources")
    if not sources:
        st.info("No sources registered.")
    else:
        mocks = sum(1 for s in sources if s["site_kind"].startswith("mock"))
        st.caption(f"{len(sources)} sources: {mocks} mock sites (ours), {len(sources) - mocks} real sites. "
                   "Check each real site's terms yourself before relying on it.")
        st.dataframe(pd.DataFrame([{
            "Market": s["market"], "Competitor": s["competitor"], "Page": s["page_type"],
            "Region": s["region"], "Kind": s["site_kind"], "URL": s["url"],
            "Every (min)": s["cadence_minutes"], "Health": s["health_score"],
            "Snapshots": s["snapshots"], "Last capture": fmt_time(s["last_captured"]),
            "Origins": ", ".join(f"{k}: {v}" for k, v in s["origins"].items()),
        } for s in sources]), hide_index=True, use_container_width=True)

    # provenance
    st.markdown("#### Where the data comes from")
    totals = data.get("origin_totals", {})
    cols = st.columns(max(len(ORIGIN_MEANING), 1))
    for col, (origin, meaning) in zip(cols, ORIGIN_MEANING.items()):
        col.markdown(origin_badge(origin), unsafe_allow_html=True)
        col.metric("Snapshots", totals.get(origin, 0))
        col.caption(meaning)
    st.caption("Marketplaces and quick-commerce apps are not scraped live. "
               "Where they appear, the data is from mocks or datasets.")

    # ledger
    st.markdown("#### Evidence integrity")
    st.caption("Every snapshot is hash-linked to the one before it. This recomputes the hashes "
               "from the stored text and screenshots; if anything was edited after capture, it shows here.")
    if st.button("Verify evidence ledger", key="ledger_btn"):
        res, e = api.get("/api/ledger/verify")
        st.session_state["ledger_res"] = res if res else {"error": e}
    res = st.session_state.get("ledger_res")
    if res:
        if "error" in res:
            st.error(res["error"])
        else:
            k1, k2, k3, k4 = st.columns(4)
            k1.metric("Snapshots checked", res["checked"])
            k2.metric("Hash matches", f"{res['hash_ok']}/{res['checked']}")
            k3.metric("Content matches", f"{res['content_ok']}/{res['checked']}")
            k4.metric("Live chain links", f"{res['chain_ok']}/{res['chain_checked']}")
            if res["content_unverifiable"]:
                st.caption(f"{res['content_unverifiable']} snapshot(s) could not have their content re-checked "
                           "because the screenshot file is missing.")
            if res["all_ok"]:
                st.success("No tampering detected in the checked snapshots.")
            else:
                st.error("Integrity problems found.")
                st.dataframe(pd.DataFrame(res["issues"]), hide_index=True, use_container_width=True)

    # audit log
    st.markdown("#### Audit log")
    events = sorted(log["counts"])
    if log["entries"]:
        pick = st.multiselect("Event types", events, default=events, key="log_events")
        rows = [e for e in log["entries"] if e["event"] in pick]
        st.caption(f"Newest first. Showing {len(rows)} of the last {len(log['entries'])} entries "
                   f"({log['total_entries']} in the file).")
        st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)