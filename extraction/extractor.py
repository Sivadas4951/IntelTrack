"""
extractor.py
------------
Stage 3a: turn a snapshot's raw_text into candidate claims.

Design (matches the IntelTrack rule "the LLM only reads, code verifies"):
  1. Rule/regex extraction per market (free, deterministic, runs first).
  2. Optional LLM structured extraction, used ONLY when the rules find fewer
     than MIN_RULE_FIELDS fields and OPENAI_API_KEY is set.
  3. Every claim leaves here with verified=0. verification/verifier.py is the
     only place that sets verified=1. A missing field is simply not emitted
     ("not found"), never guessed.

Public API (used by pipeline.py and evaluate.py):
    extract_claims(snapshot_id, source_id, market, competitor,
                   raw_text, source_url, region) -> list[dict]
"""

import os
import re
import json
import sqlite3
import hashlib
from datetime import datetime, timezone

from core.db import DB_PATH

SCHEMA_VERSION = "schema_v1"
RULE_VERSION = "rules_v1"
PROMPT_VERSION = "extract_prompt_v1"
LLM_MODEL = os.getenv("EXTRACT_LLM_MODEL", "gpt-4o-mini")
MIN_RULE_FIELDS = 3

UNITS = {
    "price_inr_per_month": "INR", "price_inr": "INR",
    "speed_mbps": "Mbps", "post_fup_speed_mbps": "Mbps",
    "fup_cap_gb": "GB", "pack_size_ml": "ml", "fat_percentage": "%",
    "battery_hours": "hours", "warranty_months": "months",
}

# Fields each market is allowed to emit (mirrors config/*.yaml schema).
MARKET_FIELDS = {
    "isp": ["plan_name", "price_inr_per_month", "speed_mbps", "fup_cap_gb",
            "post_fup_speed_mbps", "ott_bundle"],
    "dairy": ["product_name", "price_inr", "pack_size_ml", "fat_percentage"],
    "headphones": ["model_name", "price_inr", "battery_hours",
                   "anc_enabled", "warranty_months"],
}

_NUM = r"([\d][\d,]*(?:\.\d+)?)"
_PRICE = re.compile(r"₹\s*" + _NUM)
_PROMO = re.compile(r"\b(sale|offer|deal|discount|\d+\s*%\s*off|limited time)\b", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────

def _clean_num(s: str) -> str:
    return s.replace(",", "").strip()


def _search(pattern: str, text: str, flags=re.I):
    m = re.search(pattern, text, flags)
    return m


def _observed_at(snapshot_id: str) -> str:
    """Use the snapshot's captured_at so claims carry the true observation time."""
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT captured_at FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception:
        pass
    return datetime.now(timezone.utc).isoformat()


def _claim_id(snapshot_id: str, plan: str, field: str, value: str) -> str:
    h = hashlib.sha1(f"{snapshot_id}|{plan}|{field}|{value}".encode()).hexdigest()
    return f"C-{h[:8].upper()}"


def _plan_name(market: str, competitor: str, text: str) -> str | None:
    """
    Pull the plan / product / model name.
      ISP:        'SpeedFiber Fiber Ultra Plan: ...'  -> 'Fiber Ultra'
      Dairy:      'FreshDairy Fresh Whole Milk: ...'  -> 'Fresh Whole Milk'
      Headphones: 'SoundMax Pro TWS: ...'             -> 'SoundMax Pro TWS' (full)
    """
    comp = re.escape(competitor)
    m = re.search(comp + r"\s+(.+?)\s*(?::|₹|\n|\s{2,}|$)", text)
    if not m:
        return None
    name = re.sub(r"\s+Plan$", "", m.group(1).strip(), flags=re.I).strip()
    if not name or len(name) > 60:
        return None
    if market == "headphones":
        return f"{competitor} {name}" if not name.lower().startswith(competitor.lower()) else name
    return name


# ── rule extractors (return {field: (value, ambiguity)}) ────────────────────

def _price(text: str):
    m = _PRICE.search(text)
    if not m:
        return None
    window = text[max(0, m.start() - 40): m.end() + 40]
    amb = "promotional" if _PROMO.search(window) else "none"
    return _clean_num(m.group(1)), amb


def _rules_isp(text: str) -> dict:
    out = {}
    p = _price(text)
    if p:
        out["price_inr_per_month"] = p
    for m in re.finditer(r"(Post-FUP Speed|Speed)\s*:?\s*" + _NUM + r"\s*Mbps", text, re.I):
        key = "post_fup_speed_mbps" if m.group(1).lower().startswith("post") else "speed_mbps"
        out.setdefault(key, (_clean_num(m.group(2)), "none"))
    m = _search(r"FUP\s*(?:Cap|Data Limit|Limit)\s*:?\s*" + _NUM + r"\s*(?:GB|TB)", text)
    if m:
        out["fup_cap_gb"] = (_clean_num(m.group(1)), "none")
    m = _search(r"(?:Included\s+)?OTT(?:\s+Bundle)?\s*:?\s*([^.\n]+)", text)
    if m:
        items = [i.strip() for i in m.group(1).split(",") if i.strip()]
        if items:
            out["ott_bundle"] = (", ".join(items), "none")
    return out


def _rules_dairy(text: str) -> dict:
    out = {}
    p = _price(text)
    if p:
        out["price_inr"] = p
    m = _search(r"(?:Pack\s*(?:Volume|Size)|Volume|Quantity)\s*:?\s*" + _NUM + r"\s*ml", text)
    if m:
        out["pack_size_ml"] = (_clean_num(m.group(1)), "none")
    m = _search(r"Fat(?:\s*Content|\s*Percentage)?\s*:?\s*" + _NUM + r"\s*%", text)
    if m:
        out["fat_percentage"] = (_clean_num(m.group(1)), "none")
    return out


def _rules_headphones(text: str) -> dict:
    out = {}
    p = _price(text)
    if p:
        out["price_inr"] = p
    m = _search(r"(?:Playtime|Battery(?:\s*Life)?)\s*:?\s*(Up to\s*)?" + _NUM + r"\s*(?:hours?|hrs?|h)\b", text)
    if m:
        out["battery_hours"] = (_clean_num(m.group(2)), "range" if m.group(1) else "none")
    m = _search(r"(?:Active Noise Cancell?ation|ANC)\s*:?\s*(Yes|No|Enabled|Disabled)", text)
    if m:
        out["anc_enabled"] = ("true" if m.group(1).lower() in ("yes", "enabled") else "false", "none")
    m = _search(r"Warranty\s*:?\s*" + _NUM + r"\s*Months?", text)
    if m:
        out["warranty_months"] = (_clean_num(m.group(1)), "none")
    return out


_RULES = {"isp": _rules_isp, "dairy": _rules_dairy, "headphones": _rules_headphones}


# ── optional LLM fallback ───────────────────────────────────────────────────

def _llm_extract(market: str, text: str) -> dict:
    """Structured-output extraction. Returns {field: (value, 'none')}. Never raises."""
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        return {}
    fields = [f for f in MARKET_FIELDS[market] if f not in ("plan_name", "product_name", "model_name")]
    try:
        from openai import OpenAI
        client = OpenAI(api_key=key)
        prompt = (
            "Extract ONLY values that appear verbatim in the page text. "
            f"Return a JSON object with keys {fields}. Use null for anything not "
            "present. Numbers must be plain digits copied from the text; "
            "anc_enabled is 'true' or 'false'; ott_bundle is a comma-separated string.\n\n"
            f"PAGE TEXT:\n{text[:6000]}"
        )
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content)
        return {k: (str(v), "none") for k, v in data.items() if k in fields and v not in (None, "", "null")}
    except Exception as e:
        print(f"[EXTRACTOR] LLM fallback failed: {e}")
        return {}


# ── public API ──────────────────────────────────────────────────────────────

def extract_claims(
    snapshot_id: str,
    source_id: str,
    market: str,
    competitor: str,
    raw_text: str,
    source_url: str,
    region: str,
) -> list[dict]:
    if market not in _RULES:
        print(f"[EXTRACTOR] Unknown market '{market}', nothing extracted")
        return []

    text = raw_text or ""
    found = _RULES[market](text)
    method, model_ver = "rule_regex", RULE_VERSION

    if len(found) < MIN_RULE_FIELDS:
        llm_found = _llm_extract(market, text)
        if llm_found:
            for k, v in llm_found.items():
                found.setdefault(k, v)
            method, model_ver = "llm_structured", LLM_MODEL

    plan = _plan_name(market, competitor, text) or f"{competitor} default"
    name_field = {"isp": "plan_name", "dairy": "product_name", "headphones": "model_name"}[market]
    if plan != f"{competitor} default":
        found[name_field] = (plan, "none")

    observed_at = _observed_at(snapshot_id)
    claims = []
    for field in MARKET_FIELDS[market]:
        if field not in found:
            continue  # "not found": never guessed
        value, ambiguity = found[field]
        claims.append({
            "claim_id": _claim_id(snapshot_id, plan, field, value),
            "snapshot_id": snapshot_id,
            "market": market,
            "competitor": competitor,
            "plan_or_sku": plan,
            "field": field,
            "value": value,
            "unit": UNITS.get(field),
            "observed_at": observed_at,
            "source_url": source_url,
            "region": region,
            "verified": 0,                      # only the verifier may set 1
            "claim_confidence": 0.8 if ambiguity == "range" else 0.95,
            "extraction_method": method,
            "ambiguity": ambiguity,
            "cross_source_agreement": "single_source",
            "extraction_model_version": model_ver,
            "prompt_version": PROMPT_VERSION if method == "llm_structured" else None,
            "verifier_version": None,
            "schema_version": SCHEMA_VERSION,
        })
    return claims