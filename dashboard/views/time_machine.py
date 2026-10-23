"""
time_machine.py (sub-stage 5.4)
-------------------------------
Time Machine: pick one monitored page, slide between two captures, and see what changed.
Changed facts are listed with their claim IDs and highlighted in the stored page text;
screenshots are shown side by side where they exist.
"""

import html
import re

import httpx
import streamlit as st

from dashboard.components import (
    api_error, badge, fmt_time, fmt_value, origin_badge, severity_badge, stealth_badge,
)
from dashboard.evidence import evidence_lookup

MARK_OLD = "background:#f5b7b1;color:#111;padding:0 2px;border-radius:3px"
MARK_NEW = "background:#abebc6;color:#111;padding:0 2px;border-radius:3px"


@st.cache_data(ttl=60, show_spinner=False)
def _shot(base: str, snapshot_id: str):
    try:
        r = httpx.get(f"{base}/api/snapshots/{snapshot_id}/screenshot", timeout=15)
        return r.content if r.status_code == 200 else None
    except Exception:
        return None


def _highlight(text: str, values: list[str], style: str) -> str:
    """Wrap each changed value in <mark>. Best effort: only values that appear literally in the text."""
    safe = html.escape(text)
    vals = sorted({html.escape(str(v)) for v in values if v not in (None, "")}, key=len, reverse=True)
    if vals:
        pat = re.compile(r"(?<!\w)(" + "|".join(re.escape(v) for v in vals) + r")(?!\w)")
        safe = pat.sub(lambda m: f"<mark style='{style}'>{m.group(1)}</mark>", safe)
    return (f"<div style='white-space:pre-wrap;max-height:320px;overflow:auto;font-size:0.85rem;"
            f"border:1px solid #8884;border-radius:6px;padding:8px'>{safe}</div>")


def _side(api, title: str, snap: dict, values: list[str], style: str):
    st.markdown(f"**{title}** · {fmt_time(snap['captured_at'])}")
    st.markdown(origin_badge(snap["origin"]), unsafe_allow_html=True)
    img = _shot(api.base, snap["snapshot_id"]) if snap["has_screenshot"] else None
    if img:
        st.image(img, use_container_width=True)
    else:
        st.caption("No screenshot stored for this snapshot "
                   "(archived and simulated snapshots have none). Showing the stored text.")
    st.markdown(_highlight(snap["raw_text"], values, style), unsafe_allow_html=True)
    if snap.get("text_truncated"):
        st.caption("Text shortened for display.")
    st.caption(f"Snapshot {snap['snapshot_id']}")


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]
    st.subheader("Time Machine")
    st.caption("Slide between two captures of the same page. Highlights mark the facts that changed.")

    snaps, err = api.get("/api/time-machine/snapshots", market=market)
    if err:
        return api_error(err)
    if not snaps:
        return st.info("No snapshots stored for this market yet.")

    comp = st.selectbox("Competitor", sorted({s["competitor"] for s in snaps}), key="tm_comp")
    mine = [s for s in snaps if s["competitor"] == comp]
    sources = {}
    for s in mine:
        sources.setdefault(s["source_id"], s)
    src = st.selectbox("Page", list(sources), key="tm_src",
                       format_func=lambda i: f"{sources[i]['page_type']} · {sources[i]['region']} · {sources[i]['url']}")
    seq = sorted((s for s in mine if s["source_id"] == src), key=lambda s: s["captured_at"])
    if len(seq) < 2:
        return st.info("This page has only one capture so far. The Time Machine needs two.")

    labels = [f"{i + 1}. {fmt_time(s['captured_at'])} · {s['origin']}" for i, s in enumerate(seq)]
    lo, hi = st.select_slider("Compare captures (left handle = before, right = after)",
                              options=labels, value=(labels[-2], labels[-1]), key="tm_slider")
    before, after = seq[labels.index(lo)], seq[labels.index(hi)]
    if before["snapshot_id"] == after["snapshot_id"]:
        return st.warning("Both handles are on the same capture. Move one to compare.")

    res, err = api.get("/api/time-machine/compare", before=before["snapshot_id"], after=after["snapshot_id"])
    if err:
        return api_error(err)

    origins = {res["before"]["origin"], res["after"]["origin"]}
    if origins & {"simulated", "archived"}:
        st.warning("This comparison includes " + " and ".join(sorted(origins & {"simulated", "archived"}))
                   + " data, not only live captures.")

    ch, ad, rm = res["changed"], res["added"], res["removed"]
    k1, k2, k3, k4, k5 = st.columns(5)
    k1.metric("Facts changed", len(ch))
    k2.metric("Added", len(ad))
    k3.metric("Removed", len(rm))
    k4.metric("Unchanged", res["unchanged_count"])
    k5.metric("Stealth", sum(1 for c in ch if (c.get("change") or {}).get("is_stealth")))

    # changed facts
    st.markdown("#### What changed")
    if not (ch or ad or rm):
        st.info("No verified fact differs between these two captures. Any text difference is cosmetic.")
    for c in ch:
        meta = c.get("change") or {}
        with st.container(border=True):
            st.markdown(
                (stealth_badge() if meta.get("is_stealth") else "")
                + (severity_badge(meta["severity"]) if meta.get("severity") else badge("NOT CLASSIFIED", "#6b7280"))
                + f" **{c['plan_or_sku']}** · `{c['field']}`"
                + (f" · {meta['change_type']}" if meta.get("change_type") else ""),
                unsafe_allow_html=True)
            st.markdown(f"### {fmt_value(c['old_value'], c['unit'])} → {fmt_value(c['new_value'], c['unit'])}")
            st.caption(f"region {c['region']} · evidence {c['claim_id_old']} → {c['claim_id_new']}")
            evidence_lookup(api, [c["claim_id_old"], c["claim_id_new"]], key=f"tm_{c['claim_id_new']}")
    for title, items, key in (("Added", ad, "new_value"), ("Removed", rm, "old_value")):
        if items:
            with st.expander(f"{title} facts ({len(items)})"):
                for i in items:
                    cid = i.get("claim_id_new") or i.get("claim_id_old")
                    st.markdown(f"- **{i['plan_or_sku']}** · `{i['field']}` = {fmt_value(i[key], i['unit'])} "
                                f"(evidence {cid})")

    # before / after
    st.markdown("#### Before and after")
    if not res["same_source"]:
        st.caption("These captures come from different pages, so differences may not be changes.")
    old_vals = [c["old_value"] for c in ch] + [r["old_value"] for r in rm]
    new_vals = [c["new_value"] for c in ch] + [a["new_value"] for a in ad]
    left, right = st.columns(2)
    with left:
        _side(api, "Before", res["before"], old_vals, MARK_OLD)
    with right:
        _side(api, "After", res["after"], new_vals, MARK_NEW)
    st.caption("Highlights only appear where the value is written the same way in the page text.")

    # raw text differences
    td = res["text_diff"]
    with st.expander(f"Every text difference ({len(td['removed'])} removed, {len(td['added'])} added)"):
        st.caption("This is what a naive text watcher would alert on. Lines with no changed fact "
                   "above are cosmetic noise, such as banners or footer dates.")
        c1, c2 = st.columns(2)
        c1.markdown("**Removed**")
        for t in td["removed"]:
            c1.markdown(f"- {t}")
        c2.markdown("**Added**")
        for t in td["added"]:
            c2.markdown(f"- {t}")