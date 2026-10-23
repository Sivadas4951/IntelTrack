"""
config_loader.py
----------------
Loads config/value_rules.yaml and resolves field aliases.
Canonical field names (price, speed_mbps, ...) hide the fact that Person 2's
extractor and the mock site name some fields differently
(price_inr vs price_inr_per_month).
"""

import os
from functools import lru_cache

import yaml

CONFIG_PATH = os.path.join("config", "value_rules.yaml")


@lru_cache(maxsize=1)
def load_rules() -> dict:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"{CONFIG_PATH} not found. Run from the project root and "
            f"make sure value_rules.yaml is in the config/ folder."
        )
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_rules() -> dict:
    load_rules.cache_clear()
    return load_rules()


def supported_markets() -> list[str]:
    return list(load_rules().get("markets", {}).keys())


def get_market_rules(market: str) -> dict:
    markets = load_rules().get("markets", {})
    if market not in markets:
        raise KeyError(f"Unknown market '{market}'. Known: {list(markets)}")
    return markets[market]


def get_global(key: str, default=None):
    return load_rules().get(key, default)


def get_headline_rules() -> dict:
    h = load_rules().get("headline", {})
    return {
        "min_confidence": float(h.get("min_confidence", 0.7)),
        "excluded_ambiguity": list(h.get("excluded_ambiguity", [])),
    }


def get_card_defaults(market: str) -> dict:
    c = load_rules().get("cards", {})
    return {
        "expiry_days": int(c.get("default_expiry_days", 7)),
        "owner": c.get("default_owner", {}).get(market, "Strategy Lead"),
    }


def canonical_field(market: str, raw_field: str) -> str:
    """Map a raw field name (e.g. price_inr) to its canonical name (price)."""
    field_map = get_market_rules(market).get("field_map", {})
    for canon, aliases in field_map.items():
        if raw_field == canon or raw_field in aliases:
            return canon
    return raw_field


def raw_field_names(market: str, canonical: str) -> list[str]:
    """All raw names that can hold this canonical field (includes the canonical name)."""
    field_map = get_market_rules(market).get("field_map", {})
    names = list(field_map.get(canonical, []))
    if canonical not in names:
        names.append(canonical)
    return names