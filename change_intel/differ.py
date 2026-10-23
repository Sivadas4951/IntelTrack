"""
differ.py
---------
Field-level diff engine.

Compares the latest verified claims for a source against the previous
set and produces a list of raw diffs. Noise filtering and debounce
happen here before the LLM classifier sees anything.

Noise patterns stripped before comparison:
  - Footer years / copyright years
  - Cookie banner text
  - Rotating testimonials (fields marked ambiguous)
  - Timestamps and "last updated" strings
  - Whitespace normalisation
"""

import re
import sqlite3
from datetime import datetime, timezone
from core.db import DB_PATH

# Fields to always skip (cosmetic / noise)
NOISE_FIELDS = {
    "footer_year", "cookie_banner", "testimonial",
    "last_updated", "copyright_year",
}

# Regex patterns for noise in string values
NOISE_PATTERNS = [
    re.compile(r"\b20\d{2}\b"),           # years
    re.compile(r"cookie|privacy policy", re.I),
    re.compile(r"\bAll rights reserved\b", re.I),
]


def get_diffs_for_source(source_id: str) -> list[dict]:
    """
    Fetches the two most recent snapshot claim sets for a source,
    diffs them field by field, and returns a list of raw diff dicts.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get the two most recent snapshots for this source
    cursor.execute(
        """SELECT id FROM snapshots
           WHERE source_id = ?
           ORDER BY captured_at DESC LIMIT 2""",
        (source_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    if len(rows) < 2:
        return []  # Not enough history yet

    new_snap_id = rows[0][0]
    old_snap_id = rows[1][0]

    new_claims = _load_claims(new_snap_id)
    old_claims = _load_claims(old_snap_id)

    return _diff_claim_sets(old_claims, new_claims)


def _load_claims(snapshot_id: str) -> dict[str, dict]:
    """Load verified claims for a snapshot keyed by (plan_or_sku, field)."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT claim_id, plan_or_sku, field, value, unit,
                  claim_confidence, ambiguity, extraction_method
           FROM claims
           WHERE snapshot_id = ? AND verified = 1""",
        (snapshot_id,),
    )
    rows = cursor.fetchall()
    conn.close()

    claims = {}
    for row in rows:
        claim_id, plan_sku, field, value, unit, conf, ambiguity, method = row
        key = (plan_sku, field)
        claims[key] = {
            "claim_id": claim_id,
            "plan_or_sku": plan_sku,
            "field": field,
            "value": value,
            "unit": unit,
            "confidence": conf,
            "ambiguity": ambiguity,
            "method": method,
        }
    return claims


def _diff_claim_sets(
    old_claims: dict, new_claims: dict
) -> list[dict]:
    """Compare two claim sets and return raw diffs."""
    diffs = []
    all_keys = set(old_claims.keys()) | set(new_claims.keys())

    for key in all_keys:
        plan_sku, field = key

        if field in NOISE_FIELDS:
            continue

        old = old_claims.get(key)
        new = new_claims.get(key)

        def _conf(c):
            return c.get("confidence") or c.get("claim_confidence") or 0.75
        def _ambig(c):
            return c.get("ambiguity") or "none"
        def _cid(c):
            return c.get("claim_id")

        if old is None and new is not None:
            diffs.append({
                "diff_type": "added",
                "field": field,
                "plan_or_sku": plan_sku,
                "old_value": None,
                "new_value": new["value"],
                "unit": new.get("unit"),
                "claim_id_old": None,
                "claim_id_new": _cid(new),
                "confidence": _conf(new),
                "ambiguity": _ambig(new),
            })

        elif old is not None and new is None:
            diffs.append({
                "diff_type": "removed",
                "field": field,
                "plan_or_sku": plan_sku,
                "old_value": old["value"],
                "new_value": None,
                "unit": old.get("unit"),
                "claim_id_old": _cid(old),
                "claim_id_new": None,
                "confidence": _conf(old),
                "ambiguity": _ambig(old),
            })

        elif old is not None and new is not None:
            if _values_differ(old["value"], new["value"], field):
                diffs.append({
                    "diff_type": "changed",
                    "field": field,
                    "plan_or_sku": plan_sku,
                    "old_value": old["value"],
                    "new_value": new["value"],
                    "unit": new.get("unit"),
                    "claim_id_old": _cid(old),
                    "claim_id_new": _cid(new),
                    "confidence": min(_conf(old), _conf(new)),
                    "ambiguity": _ambig(new),
                })

    return diffs


def _values_differ(old: str, new: str, field: str) -> bool:
    """Return True if the two values represent a meaningful difference."""
    if old == new:
        return False

    # Normalise whitespace
    old_n = " ".join(old.split())
    new_n = " ".join(new.split())
    if old_n == new_n:
        return False

    # Numeric comparison: treat 1500.0 == 1500
    try:
        old_f = float(old_n.replace(",", ""))
        new_f = float(new_n.replace(",", ""))
        return abs(old_f - new_f) > 1e-6
    except ValueError:
        pass

    # String: check if only noise patterns differ
    old_stripped = _strip_noise(old_n)
    new_stripped = _strip_noise(new_n)
    return old_stripped.lower() != new_stripped.lower()


def _strip_noise(text: str) -> str:
    for pat in NOISE_PATTERNS:
        text = pat.sub("", text)
    return text.strip()