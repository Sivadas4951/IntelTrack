"""
value_map.py (sub-stage 4.3)
----------------------------
Founder dashboard item 4: value map. Weighted feature score (x) against price (y),
with a fair-value line. Model lane: computed from verified claims, not an observed fact.
Plans missing any needed fact are listed with the reason, never guessed.
"""

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.components import api_error, lane_caption
from dashboard.evidence import evidence_lookup

POS_COLOR = {"above": "#c0392b", "below": "#1e8449", "on_line": "#6b7280", "no_line": "#2e6da4"}
POS_TEXT = {
    "above": "above line: charges more than its features explain",
    "below": "below line: charges less than its features explain",
    "on_line": "on the line (within 5%)",
    "no_line": "no fair-value line",
}


def _chart(vm: dict, use_line: bool):
    pts = pd.DataFrame(vm["points"])
    if not use_line:
        pts["position"] = "no_line"
    base = alt.Chart(pts).mark_circle(size=140).encode(
        x=alt.X("feature_score:Q", title="Feature score (0 to 100, relative to plans shown)",
                scale=alt.Scale(domain=[-5, 105])),
        y=alt.Y("price:Q", title="Price", scale=alt.Scale(zero=False)),
        color=alt.Color("position:N", legend=alt.Legend(title="Position"),
                        scale=alt.Scale(domain=list(POS_COLOR), range=list(POS_COLOR.values()))),
        tooltip=["name", "price", "feature_score", "fair_price", "gap_pct", "position"],
    )
    labels = alt.Chart(pts).mark_text(align="left", dx=9, dy=-6, fontSize=11).encode(
        x="feature_score:Q", y="price:Q", text="name:N")
    layers = [base, labels]
    if use_line and vm.get("line"):
        ln = vm["line"]
        line_df = pd.DataFrame({"feature_score": [0, 100],
                                "price": [ln["intercept"], ln["intercept"] + ln["slope"] * 100]})
        layers.insert(0, alt.Chart(line_df).mark_line(strokeDash=[6, 4], color="#444")
                      .encode(x="feature_score:Q", y="price:Q"))
    return alt.layer(*layers).properties(height=420)


def render(ctx: dict):
    api, market = ctx["api"], ctx["market"]
    region = None if ctx["region"] == "All" else ctx["region"]

    st.subheader("Value map")
    st.caption("Does a plan charge more or less than its features explain? "
               "Each dot is a plan; the dashed line is the fair price for a given feature score.")
    lane_caption("model")

    vm, err = api.get("/api/value-map", market=market, region=region)
    if err:
        return api_error(err)

    pts = vm.get("points", [])
    if len(pts) < 2:
        for w in vm.get("warnings", []):
            st.warning(w)
        st.info("Not enough plans with every needed fact to draw a value map.")
        _excluded(vm)
        return

    line = vm.get("line")
    use_line = bool(line) and line["slope"] > 0

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Plans compared", vm["n_points"])
    c2.metric("Excluded (missing facts)", len(vm.get("excluded", [])))
    c3.metric("Line fit (R²)", "n/a" if not line or line.get("r_squared") is None else line["r_squared"])
    c4.metric("Price per score point", "n/a" if not line else round(line["slope"], 2))

    for w in vm.get("warnings", []):
        st.warning(w)
    if line and not use_line:
        st.error("The fitted line slopes downward in this sample, so gap figures are hidden.")

    st.altair_chart(_chart(vm, use_line), use_container_width=True)
    if use_line:
        st.caption("Red dots charge more than the line predicts; green dots charge less. "
                   "The line only describes the plans shown here, so it is not a universal price.")

    with st.expander("How the score is built"):
        st.write(vm.get("method"))
        st.write("Group weights (renormalised over the groups that vary):", vm.get("weights") or "n/a")
        feats = vm.get("features", {})
        if feats:
            st.dataframe(pd.DataFrame([{"Feature": f, "Group": g} for f, g in feats.items()]),
                         hide_index=True, use_container_width=True)
        if vm.get("skipped_features"):
            st.caption("Skipped because every plan has the same value (no information): "
                       + ", ".join(vm["skipped_features"]))

    st.markdown("#### Plans, ranked by feature score")
    rows = []
    for p in reversed(pts):
        gs = p.get("group_scores", {})
        rows.append({
            "Plan": p["name"], "Price": p["price"], "Feature score": p["feature_score"],
            "Quantity score": gs.get("quantity"), "Bundle score": gs.get("bundle"),
            "Fair price": p["fair_price"] if use_line else None,
            "Gap %": p["gap_pct"] if use_line else None,
            "Position": POS_TEXT.get(p["position"] if use_line else "no_line"),
            "Origins": ", ".join(p.get("origins", [])),
        })
    st.dataframe(pd.DataFrame(rows), use_container_width=True, hide_index=True)

    st.markdown("#### Evidence")
    pick = st.selectbox("Plan", [p["name"] for p in pts], key="vm_pick")
    chosen = next(p for p in pts if p["name"] == pick)
    st.caption(f"{len(chosen['evidence_claim_ids'])} claims behind this point (price and every feature input).")
    evidence_lookup(api, chosen["evidence_claim_ids"], key=f"vm_ev_{pick}")

    _excluded(vm)


def _excluded(vm: dict):
    ex = vm.get("excluded", [])
    if not ex:
        return
    with st.expander(f"Not on the map: {len(ex)} plan(s) with missing facts"):
        st.caption("These are left out rather than guessed. Fix the data and they appear.")
        st.dataframe(pd.DataFrame([{"Plan": e["name"], "Reason": e["reason"],
                                    "Claim IDs": ", ".join(e.get("claim_ids", []))} for e in ex]),
                     hide_index=True, use_container_width=True)