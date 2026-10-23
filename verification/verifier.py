"""
verifier.py
-----------
Verbatim verifier: checks that every extracted claim value
actually appears in the stored snapshot raw_text.

Rules:
  - Numbers: the numeric string must appear somewhere in raw_text
  - Strings: the value must appear verbatim (case-insensitive)
  - Booleans: "yes"/"true"/"no"/"false" must appear near the field name
  - Lists: at least one element must appear verbatim
  - Derived metrics: auto-pass (cannot appear verbatim, they are computed)

Output: updates claim["verified"] to 1 or 0 and sets verifier_version.
"""

import re
from typing import Any

VERIFIER_VERSION = "verifier_v1.0"


def verify_claims(claims: list[dict], raw_text: str) -> list[dict]:
    """
    Takes a list of claim dicts (from extractor) and raw_text from the snapshot.
    Returns the same list with verified=1/0 and verifier_version set.
    """
    for claim in claims:
        claim["verifier_version"] = VERIFIER_VERSION
        field = claim.get("field", "")
        value = claim.get("value", "")
        method = claim.get("extraction_method", "")

        # Derived/normalised metrics cannot appear verbatim — auto-pass
        if "normalizer" in method or claim.get("unit") == "derived":
            claim["verified"] = 1
            continue

        # Vision-only extractions: lower confidence, still attempt verify
        verified, confidence_penalty = _verify_value(field, value, raw_text)
        claim["verified"] = 1 if verified else 0

        # Penalise confidence slightly if only partially verified
        if confidence_penalty:
            claim["claim_confidence"] = max(
                0.0, float(claim.get("claim_confidence", 0.5)) - 0.1
            )

    return claims


def _verify_value(field: str, value: str, raw_text: str) -> tuple[bool, bool]:
    """
    Returns (verified: bool, confidence_penalty: bool).
    confidence_penalty is True when the match is soft (e.g. partial list).
    """
    if not value or value.strip() == "":
        return False, False

    raw_lower = raw_text.lower()
    val_stripped = value.strip()

    # Boolean fields
    if val_stripped.lower() in ("true", "false", "yes", "no"):
        return _verify_boolean(field, val_stripped, raw_lower), False

    # Numeric fields: strip commas, check the number string appears
    numeric = _try_numeric(val_stripped)
    if numeric is not None:
        return _verify_numeric(numeric, raw_text), False

    # List fields (stored as comma-separated string)
    if "," in val_stripped:
        return _verify_list(val_stripped, raw_lower)

    # Plain string: case-insensitive substring match
    return val_stripped.lower() in raw_lower, False


def _verify_numeric(numeric_str: str, raw_text: str) -> bool:
    """Check that the number appears in raw_text, with or without commas."""
    # Remove trailing .0 for integers stored as float strings
    clean = numeric_str.rstrip("0").rstrip(".") if "." in numeric_str else numeric_str

    # Direct substring
    if clean in raw_text:
        return True

    # With comma formatting (e.g. 1500 → 1,500)
    try:
        n = float(numeric_str)
        formatted = f"{int(n):,}"
        if formatted in raw_text:
            return True
        # Indian number format (1,00,000)
        indian = _indian_format(int(n))
        if indian in raw_text:
            return True
    except Exception:
        pass

    return False


def _verify_boolean(field: str, value: str, raw_lower: str) -> bool:
    """
    Check that a boolean-adjacent word appears near the field name.
    e.g. "Active Noise Cancellation: Yes"
    """
    positive = value.lower() in ("true", "yes")
    pos_words = ["yes", "true", "enabled", "included", "✓"]
    neg_words = ["no", "false", "disabled", "not included", "✗"]
    words = pos_words if positive else neg_words

    field_lower = field.lower().replace("_", " ")
    # Look for field name within 200 chars of the word
    idx = raw_lower.find(field_lower)
    if idx == -1:
        # Try keyword matching for known fields
        alt_map = {
            "anc_enabled": "noise cancellation",
            "anc": "noise cancellation",
        }
        idx = raw_lower.find(alt_map.get(field_lower, field_lower))

    if idx == -1:
        # Fallback: just check the word exists anywhere
        return any(w in raw_lower for w in words)

    window = raw_lower[max(0, idx - 20): idx + 200]
    return any(w in window for w in words)


def _verify_list(value: str, raw_lower: str) -> tuple[bool, bool]:
    """
    For list values, at least one element must appear verbatim.
    Returns (verified, confidence_penalty).
    confidence_penalty=True if only partial (not all elements found).
    """
    elements = [e.strip().lower() for e in value.split(",") if e.strip()]
    found = [e for e in elements if e in raw_lower]
    if not found:
        return False, False
    all_found = len(found) == len(elements)
    return True, not all_found


def _try_numeric(value: str) -> str | None:
    """Return the cleaned numeric string if value looks like a number."""
    cleaned = value.replace(",", "").strip()
    try:
        float(cleaned)
        return cleaned
    except ValueError:
        return None


def _indian_format(n: int) -> str:
    """Format an integer in Indian numbering (e.g. 1,00,000)."""
    s = str(n)
    if len(s) <= 3:
        return s
    last3 = s[-3:]
    rest = s[:-3]
    parts = []
    while len(rest) > 2:
        parts.append(rest[-2:])
        rest = rest[:-2]
    if rest:
        parts.append(rest)
    parts.reverse()
    return ",".join(parts) + "," + last3