"""
components.py
-------------
Small shared UI helpers used by every dashboard view.
"""

from datetime import datetime

import pandas as pd
import streamlit as st

SEV_COLOR = {"high": "#c0392b", "medium": "#b9770e", "low": "#6b7280"}
ORIGIN_COLOR = {"live": "#1e8449", "archived": "#2e6da4", "simulated": "#8e44ad"}


def badge(text: str, color: str) -> str:
    return (f"<span style='background:{color};color:#fff;padding:2px 8px;border-radius:10px;"
            f"font-size:0.75rem;font-weight:600;margin-right:4px'>{text}</span>")


def severity_badge(sev: str) -> str:
    return badge(str(sev).upper(), SEV_COLOR.get(sev, "#6b7280"))


def stealth_badge() -> str:
    return badge("STEALTH", "#6c3483")


def origin_badge(origin: str) -> str:
    return badge(str(origin).upper(), ORIGIN_COLOR.get(origin, "#6b7280"))


def fmt_value(value, unit=None) -> str:
    """'not found' is shown explicitly; values are never guessed or filled in."""
    if value is None or value == "":
        return "not found"
    if unit == "INR":
        return f"₹{value}"
    if unit and unit != "derived":
        return f"{value} {unit}"
    return str(value)


def fmt_time(iso: str | None) -> str:
    if not iso:
        return "-"
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).astimezone().strftime("%d %b %H:%M")
    except ValueError:
        return str(iso)


def changes_to_df(changes: list[dict]) -> pd.DataFrame:
    rows = []
    for c in changes:
        rows.append({
            "Detected": fmt_time(c.get("detected_at")),
            "Severity": str(c.get("severity", "")).upper(),
            "Stealth": "yes" if c.get("is_stealth") else "",
            "Competitor": c.get("competitor"),
            "Plan / SKU": c.get("plan_or_sku"),
            "Field": c.get("field"),
            "Before": fmt_value(c.get("old_value"), c.get("unit")),
            "After": fmt_value(c.get("new_value"), c.get("unit")),
            "Type": c.get("change_type"),
            "Region": c.get("region"),
            "Evidence (old → new)": f"{c.get('claim_id_old') or '-'} → {c.get('claim_id_new') or '-'}",
        })
    return pd.DataFrame(rows)


def lane_caption(lane: str):
    """Two-lane rule: the UI always says which lane a panel belongs to."""
    if lane == "evidence":
        st.caption("Evidence lane: every row is a verified claim linked to a stored snapshot.")
    else:
        st.caption("Model lane: computed or inferred from verified claims. Not an observed fact.")


def api_error(err: str):
    st.error(err)
    st.caption("Start the API with: uvicorn api.main:app --port 8000")