"""
dashboard/app.py
----------------
IntelTrack founder dashboard (Streamlit). Run from the project root:

    streamlit run dashboard/app.py

The API must be running first:  uvicorn api.main:app --port 8000

Each tab is a module in dashboard/views/ exposing render(ctx). A tab whose module
does not exist yet shows a placeholder, so sub-stages 4.2 to 4.4 and stage 5 just
drop their files in without editing this one.
"""

import os
import sys
import time
import importlib

# Streamlit puts dashboard/ on sys.path, not the project root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import streamlit as st

from dashboard.api_client import Api, DEFAULT_API_URL
from dashboard.components import api_error

st.set_page_config(page_title="IntelTrack", page_icon="📡", layout="wide")

# (tab label, module name, when it is built)
TABS = [
    ("Overview", "overview", "4.1"),
    ("Leaderboard", "leaderboard", "4.2"),
    ("Volatility", "volatility", "4.2"),
    ("Value map", "value_map", "4.3"),
    ("Timelines", "timelines", "4.3"),
    ("Counter-Moves", "cards", "4.4"),
    ("Reports", "reports", "4.4"),
    ("Time Machine", "time_machine", "stage 5"),
    ("Accuracy", "accuracy", "stage 5"),
    ("Compliance", "compliance", "stage 5"),
]

# ── sidebar ──────────────────────────────────────────────────────────────────
st.sidebar.title("IntelTrack")
st.sidebar.caption("Evidence-first competitor intelligence")

api_url = st.sidebar.text_input("API URL", os.getenv("RIVALIQ_API_URL", DEFAULT_API_URL))
api = Api(api_url)

markets, err = api.get("/api/markets")
if err or not markets:
    api_error(err or "API returned no markets.")
    st.stop()

market = st.sidebar.selectbox(
    "Market", [m["market"] for m in markets],
    format_func=lambda m: {"isp": "ISP", "dairy": "Dairy", "headphones": "Headphones / earbuds"}.get(m, m),
)
entry = next(m for m in markets if m["market"] == market)
region = st.sidebar.selectbox("Region", ["All"] + entry["regions"])
hours = st.sidebar.selectbox(
    "Window", [24, 72, 168, 720], index=0,
    format_func=lambda h: f"Last {h}h" if h < 168 else f"Last {h // 24} days",
)

st.sidebar.divider()
if st.sidebar.button("Re-run analysis"):
    res, e = api.post("/api/refresh")
    st.sidebar.success(f"Analysed {res['changes_analysed']} change(s)") if res else st.sidebar.error(e)
auto = st.sidebar.toggle("Auto-refresh")
interval = st.sidebar.slider("Every (seconds)", 10, 120, 30, disabled=not auto)

ctx = {
    "api": api, "market": market, "region": region, "hours": hours,
    "personas": entry["personas"], "metrics": entry.get("metrics", []),
    "primary_metric": entry.get("primary_metric"),
}

# ── tabs ─────────────────────────────────────────────────────────────────────
st.title("Founder dashboard")
tabs = st.tabs([t[0] for t in TABS])
for tab, (label, module, built_in) in zip(tabs, TABS):
    with tab:
        try:
            view = importlib.import_module(f"dashboard.views.{module}")
        except ModuleNotFoundError as e:
            if e.name == f"dashboard.views.{module}":
                st.info(f"{label} is built in {built_in}.")
                continue
            raise
        view.render(ctx)

if auto:
    time.sleep(interval)
    st.rerun()