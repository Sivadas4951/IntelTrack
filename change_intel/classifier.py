"""
classifier.py
-------------
Takes raw diffs from differ.py and classifies each one:
  - change_type: price | feature | quantity | bundle | positioning | announcement | cosmetic
  - severity: high | medium | low
  - is_stealth: True if price unchanged but customer value decreased

Primary classifier: rule-based (fast, no API cost).
LLM classifier: only called for diffs the rules cannot confidently classify.
"""

import os
import json
import uuid
import sqlite3
from datetime import datetime, timezone
from core.db import DB_PATH

# ── Rule tables ──────────────────────────────────────────────────────────────

PRICE_FIELDS = {
    "price_inr", "price_inr_per_month", "effective_monthly_cost",
    "cost_per_mbps", "cost_per_litre", "cost_per_battery_hour",
}

QUANTITY_FIELDS = {
    "fup_cap_gb", "pack_size_ml", "battery_hours", "speed_mbps",
    "post_fup_speed_mbps", "warranty_months",
}

BUNDLE_FIELDS = {"ott_bundle", "anc_enabled"}

POSITIONING_FIELDS = {
    "plan_name", "model_name", "product_name", "tagline", "hero_copy",
}

# Severity thresholds for numeric changes (percentage)
HIGH_THRESHOLD_PCT = 5.0
MEDIUM_THRESHOLD_PCT = 1.0


def classify_diffs(
    diffs: list[dict],
    market: str,
    competitor: str,
) -> list[dict]:
    """
    Takes raw diffs, classifies each, detects stealth, writes to `changes` table.
    Returns list of change dicts ready for alert routing.
    """
    if not diffs:
        return []

    price_changed = any(d["field"] in PRICE_FIELDS for d in diffs)
    changes = []

    for diff in diffs:
        change_type = _classify_type(diff)
        severity = _classify_severity(diff, change_type)

        # Stealth: non-price field worsened while price did NOT change
        is_stealth = (
            not price_changed
            and change_type in ("feature", "quantity", "bundle")
            and _is_worsening(diff)
        )

        # Try LLM for uncertain cases
        if change_type == "unknown" and os.getenv("OPENAI_API_KEY"):
            change_type, severity = _llm_classify(diff, market)

        change = {
            "id": f"chg_{uuid.uuid4().hex[:12]}",
            "claim_id_old": diff.get("claim_id_old"),
            "claim_id_new": diff.get("claim_id_new"),
            "field": diff["field"],
            "change_type": change_type,
            "severity": severity,
            "is_stealth": 1 if is_stealth else 0,
            "detected_at": datetime.now(timezone.utc).isoformat(),
            "status": "pending",
            # Extra context for alert router (not in DB schema)
            "_diff": diff,
            "_market": market,
            "_competitor": competitor,
        }
        changes.append(change)

    _write_changes_to_db(changes)
    return changes


def _classify_type(diff: dict) -> str:
    field = diff["field"]
    if field in PRICE_FIELDS:
        return "price"
    if field in QUANTITY_FIELDS:
        return "quantity"
    if field in BUNDLE_FIELDS:
        return "bundle"
    if field in POSITIONING_FIELDS:
        return "positioning"
    return "unknown"


def _classify_severity(diff: dict, change_type: str) -> str:
    if change_type == "cosmetic":
        return "low"

    old_v = diff.get("old_value")
    new_v = diff.get("new_value")

    # Removal of a plan or feature = high
    if diff["diff_type"] == "removed":
        return "high"

    # Numeric severity by percentage change
    if old_v is not None and new_v is not None:
        try:
            old_f = float(str(old_v).replace(",", ""))
            new_f = float(str(new_v).replace(",", ""))
            if old_f != 0:
                pct = abs((new_f - old_f) / old_f) * 100
                if pct >= HIGH_THRESHOLD_PCT:
                    return "high"
                if pct >= MEDIUM_THRESHOLD_PCT:
                    return "medium"
                return "low"
        except ValueError:
            pass

    # Boolean flip (e.g. ANC removed)
    if str(old_v).lower() in ("true", "yes") and str(new_v).lower() in ("false", "no"):
        return "high"

    if change_type == "price":
        return "medium"
    if change_type in ("quantity", "bundle"):
        return "medium"

    return "low"


def _is_worsening(diff: dict) -> bool:
    """True if the change represents a decrease in customer value."""
    field = diff["field"]
    old_v = diff.get("old_value")
    new_v = diff.get("new_value")

    if old_v is None or new_v is None:
        return False

    # Numeric: lower is worse for quantity fields
    if field in QUANTITY_FIELDS:
        try:
            return float(str(new_v).replace(",", "")) < float(str(old_v).replace(",", ""))
        except ValueError:
            pass

    # Boolean: True→False is worse (e.g. ANC removed)
    if str(old_v).lower() in ("true", "yes") and str(new_v).lower() in ("false", "no"):
        return True

    # List: fewer items is worse
    if "," in str(old_v):
        old_len = len([x for x in str(old_v).split(",") if x.strip()])
        new_len = len([x for x in str(new_v).split(",") if x.strip()])
        return new_len < old_len

    return False


def _llm_classify(diff: dict, market: str) -> tuple[str, str]:
    """LLM fallback for unclassified diffs. Returns (change_type, severity)."""
    try:
        from openai import OpenAI
        client = OpenAI()
        prompt = (
            f"Market: {market}\n"
            f"Field: {diff['field']}\n"
            f"Old value: {diff.get('old_value')}\n"
            f"New value: {diff.get('new_value')}\n"
            f"Diff type: {diff['diff_type']}\n\n"
            "Classify this competitor change.\n"
            "Return JSON with exactly two keys:\n"
            '  "change_type": one of price|feature|quantity|bundle|positioning|announcement|cosmetic\n'
            '  "severity": one of high|medium|low\n'
            "Return only JSON."
        )
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return data.get("change_type", "unknown"), data.get("severity", "medium")
    except Exception:
        return "unknown", "medium"


def _write_changes_to_db(changes: list[dict]):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for c in changes:
        cursor.execute(
            """INSERT OR IGNORE INTO changes
               (id, claim_id_old, claim_id_new, field, change_type,
                severity, is_stealth, detected_at, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                c["id"], c.get("claim_id_old"), c.get("claim_id_new"),
                c["field"], c["change_type"], c["severity"],
                c["is_stealth"], c["detected_at"], c["status"],
            ),
        )
    conn.commit()
    conn.close()