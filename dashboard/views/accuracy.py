"""
accuracy.py (sub-stage 5.2)
---------------------------
Accuracy tab: the numbers measured by `python evaluate.py`, shown as they are,
including the weak spots. Nothing here is typed in by hand; it all comes from
data/eval_results.json via /api/evaluation.
"""

import math

import altair as alt
import pandas as pd
import streamlit as st

from dashboard.components import fmt_time


def _wilson(k: int, n: int):
    """95% Wilson interval. With ~40 test pages the uncertainty is real, so we show it."""
    if n == 0:
        return None
    z, p = 1.96, k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return max(0.0, c - h), min(1.0, c + h)


def _pct(x):
    return "n/a" if x is None else f"{x * 100:.1f}%"


def _ci(k, n):
    w = _wilson(k, n)
    return "n/a" if w is None else f"{w[0] * 100:.0f}% to {w[1] * 100:.0f}%"


def _prf(df: pd.DataFrame, col: str) -> dict:
    real = df["is_real_change"].astype(bool)
    hit = df[col].astype(bool)
    tp, fp, fn = int((real & hit).sum()), int((~real & hit).sum()), int((real & ~hit).sum())
    return {"tp": tp, "fp": fp, "fn": fn,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None, "alerts": tp + fp}


def render(ctx: dict):
    api = ctx["api"]
    st.subheader("Accuracy")
    st.caption("Measured on mutated test pages with a known answer: about half real changes, "
               "half noise (cookie banners, footer dates, rotating testimonials). "
               "A naive 'alert on any text change' system is the baseline.")

    data, err = api.get("/api/evaluation")
    if err:
        st.info("No evaluation results yet.")
        st.code("python evaluate.py", language="bash")
        st.caption(f"API said: {err}")
        return

    s, cases = data["summary"], pd.DataFrame(data.get("cases", []))
    nv, rq = s["naive"], s["rivaliq"]
    st.caption(f"Generated {fmt_time(data.get('generated_at'))} · {s['total']} test pages "
               f"({s['real']} real changes, {s['noise']} noise). "
               "Re-run evaluate.py after changing thresholds so this tab stays current.")

    # headline numbers
    a, b, c = st.columns(3)
    a.metric("Precision", _pct(rq["precision"]),
             f"{(rq['precision'] - nv['precision']) * 100:+.1f} pts vs naive")
    b.metric("Recall", _pct(rq["recall"]),
             f"{(rq['recall'] - nv['recall']) * 100:+.1f} pts vs naive")
    n_alerts, r_alerts = nv["tp"] + nv["fp"], rq["tp"] + rq["fp"]
    c.metric("Alerts raised", r_alerts, f"{r_alerts - n_alerts:+d} vs naive", delta_color="inverse")
    d, e, f = st.columns(3)
    d.metric("False alarms", rq["fp"], f"{rq['fp'] - nv['fp']:+d} vs naive", delta_color="inverse")
    e.metric("Stealth-change recall", _pct(s.get("stealth_recall")))
    f.metric("Extraction verified", f"{s.get('extraction_verified_pct', 0):.1f}%")

    # side-by-side table with uncertainty
    st.markdown("#### IntelTrack vs naive baseline")
    rows = []
    for name, m in (("Naive (any text change)", nv), ("IntelTrack", rq)):
        rows.append({
            "System": name, "Alerts": m["tp"] + m["fp"], "True positives": m["tp"],
            "False positives": m["fp"], "Missed (false negatives)": m["fn"],
            "Precision": _pct(m["precision"]), "Precision 95% range": _ci(m["tp"], m["tp"] + m["fp"]),
            "Recall": _pct(m["recall"]), "Recall 95% range": _ci(m["tp"], m["tp"] + m["fn"]),
        })
    st.dataframe(pd.DataFrame(rows), hide_index=True, use_container_width=True)
    st.caption("The 95% range shows how much a result could move on a test set this small. "
               "Wide ranges mean the number is a rough guide, not a guarantee.")

    long = pd.DataFrame([
        {"System": "Naive", "Metric": "Precision", "Value": nv["precision"]},
        {"System": "Naive", "Metric": "Recall", "Value": nv["recall"]},
        {"System": "IntelTrack", "Metric": "Precision", "Value": rq["precision"]},
        {"System": "IntelTrack", "Metric": "Recall", "Value": rq["recall"]},
    ])
    st.altair_chart(
        alt.Chart(long).mark_bar().encode(
            x=alt.X("Metric:N", title=None), xOffset="System:N",
            y=alt.Y("Value:Q", scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%"), title=None),
            color=alt.Color("System:N", scale=alt.Scale(domain=["Naive", "IntelTrack"], range=["#9ca3af", "#2e6da4"])),
            tooltip=["System", "Metric", alt.Tooltip("Value:Q", format=".1%")],
        ).properties(height=240), use_container_width=True)

    if cases.empty:
        return st.info("The results file has no per-case rows, so the breakdown below is unavailable.")

    # consistency with the stored summary
    mine = _prf(cases, "rivaliq_alerted")
    if (mine["tp"], mine["fp"], mine["fn"]) != (rq["tp"], rq["fp"], rq["fn"]):
        st.warning("The per-case rows do not add up to the stored summary. "
                   "Re-run evaluate.py before quoting these numbers.")

    # per market
    st.markdown("#### By market")
    mrows = []
    for mk, g in cases.groupby("market"):
        n, r = _prf(g, "naive_alerted"), _prf(g, "rivaliq_alerted")
        mrows.append({
            "Market": mk, "Pages": len(g), "Real": int(g["is_real_change"].sum()),
            "Naive alerts": n["alerts"], "IntelTrack alerts": r["alerts"],
            "Naive precision": _pct(n["precision"]), "IntelTrack precision": _pct(r["precision"]),
            "Naive recall": _pct(n["recall"]), "IntelTrack recall": _pct(r["recall"]),
        })
    st.dataframe(pd.DataFrame(mrows), hide_index=True, use_container_width=True)

    # weak spots
    st.markdown("#### Weak spots")
    missed = cases[cases["is_real_change"].astype(bool) & ~cases["rivaliq_alerted"].astype(bool)]
    false_alarms = cases[~cases["is_real_change"].astype(bool) & cases["rivaliq_alerted"].astype(bool)]
    stealth_missed = cases[cases["label"].str.contains("stealth", case=False, na=False)
                           & cases["is_real_change"].astype(bool) & ~cases["stealth_flagged"].astype(bool)]

    w1, w2, w3 = st.columns(3)
    w1.metric("Real changes IntelTrack missed", len(missed))
    w2.metric("Noise IntelTrack alerted on", len(false_alarms))
    w3.metric("Stealth cases not flagged", len(stealth_missed))

    cols = ["market", "label", "diffs_found", "changes_classified", "ttd_ms"]
    for title, df in (("Missed real changes", missed), ("False alarms", false_alarms),
                      ("Stealth cases not flagged as stealth", stealth_missed)):
        with st.expander(f"{title} ({len(df)})"):
            if df.empty:
                st.success("None in this test set.")
            else:
                st.dataframe(df[cols], hide_index=True, use_container_width=True)

    # time to detect, said plainly
    st.markdown("#### Time to detect")
    st.metric("Average processing time per page", f"{s.get('avg_ttd_ms', 0):.1f} ms")
    st.caption("This is how long the pipeline takes to process a page once it has it. "
               "It is not the delay before a real site is next checked, which depends on the "
               "capture cadence (minutes to hours).")

    with st.expander("All test cases"):
        only = st.radio("Show", ["all", "real changes", "noise"], horizontal=True, key="acc_filter")
        view = cases if only == "all" else cases[cases["is_real_change"].astype(bool) == (only == "real changes")]
        st.dataframe(view, hide_index=True, use_container_width=True)