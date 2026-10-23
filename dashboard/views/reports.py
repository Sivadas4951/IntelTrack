"""
reports.py (sub-stage 4.4)
--------------------------
Sourced reports. Every sentence was built from claim IDs; the checker result is shown
with the report, and the lane (evidence or model) is shown on every sentence.
"""

from itertools import groupby

import streamlit as st

from dashboard.components import api_error, badge, fmt_time, origin_badge
from dashboard.evidence import claim_ids_caption, evidence_lookup

LANE_COLOR = {"evidence": "#1e8449", "model": "#2e6da4"}


def _markdown(rep: dict) -> str:
    """Plain-text export: every line keeps its claim IDs."""
    out = [f"# {rep.get('title', 'Report')}", ""]
    if rep.get("disclosure"):
        out += [f"> {rep['disclosure']}", ""]
    for section, items in groupby(rep.get("sentences", []), key=lambda s: s.get("section", "")):
        out += [f"## {section}", ""]
        for s in items:
            out.append(f"- {s['text']}  \n  _[{s.get('lane')}] claims: {', '.join(s.get('claim_ids', []))}_")
        out.append("")
    return "\n".join(out)


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]

    st.subheader("Reports")
    st.caption("No sentence appears without a claim ID. Open any section to see the evidence behind it.")

    rows, err = api.get("/api/reports", market=market)
    if err:
        return api_error(err)
    if not rows:
        st.info("No reports stored for this market yet.")
        return

    types = sorted({r["report_type"] for r in rows})
    rtype = st.selectbox("Report type", ["all"] + types, key="rep_type")
    shown = [r for r in rows if rtype == "all" or r["report_type"] == rtype]
    pick = st.selectbox(
        "Report", shown, key="rep_pick",
        format_func=lambda r: f"{r['report_type']} · {r['subject']} · {fmt_time(r['created_at'])}",
    )
    if not pick:
        return
    rep = pick["report"]

    ok = bool(pick.get("checker_passed"))
    st.markdown(
        badge("CHECKER PASSED" if ok else "CHECKER FAILED", "#1e8449" if ok else "#c0392b")
        + "".join(origin_badge(o) for o in rep.get("origins", [])),
        unsafe_allow_html=True,
    )
    st.markdown(f"### {rep.get('title', pick['subject'])}")
    st.caption(f"{pick['sentence_count']} sentences · created {fmt_time(pick['created_at'])} · "
               f"report {pick['report_id']}")
    if not ok:
        st.error("This report failed the sentence checker. Treat its content as unverified.")
    if rep.get("disclosure"):
        st.warning(rep["disclosure"])
    if rep.get("lane_note"):
        st.caption(rep["lane_note"])

    sentences = rep.get("sentences", [])
    for section, items in groupby(sentences, key=lambda s: s.get("section", "")):
        items = list(items)
        with st.expander(f"{section} ({len(items)})", expanded=True):
            for s in items:
                lane = s.get("lane", "")
                st.markdown(f"{badge(lane.upper(), LANE_COLOR.get(lane, '#6b7280'))} {s.get('text', '')}",
                            unsafe_allow_html=True)
                claim_ids_caption(s.get("claim_ids", []))
            evidence_lookup(api, [i for s in items for i in s.get("claim_ids", [])],
                            key=f"rep_{pick['report_id']}_{section}")

    gaps = rep.get("gaps", [])
    if gaps:
        with st.expander(f"Gaps: {len(gaps)} thing(s) the report could not say"):
            st.caption("Left out because a needed fact was not found. Nothing was guessed.")
            for g in gaps:
                st.markdown(f"- {g.get('text') or g.get('reason') or g}" if isinstance(g, dict) else f"- {g}")

    st.download_button("Download as Markdown", _markdown(rep),
                       file_name=f"{pick['report_id']}.md", mime="text/markdown")