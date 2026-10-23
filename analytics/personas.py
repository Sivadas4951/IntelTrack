"""
personas.py
-----------
Loads config/personas.yaml and checks whether a plan meets a persona's needs.

Three outcomes per persona check, never a guess:
  True   every requirement is met by a verified fact
  False  at least one requirement is not met
  None   no failure, but a needed fact is "not found" (cannot verify)
"""

import operator
import os
from functools import lru_cache

import yaml

PERSONA_PATH = os.path.join("config", "personas.yaml")

_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq}


@lru_cache(maxsize=1)
def load_persona_config() -> dict:
    if not os.path.exists(PERSONA_PATH):
        raise FileNotFoundError(
            f"{PERSONA_PATH} not found. Run from the project root and put "
            f"personas.yaml in the config/ folder."
        )
    with open(PERSONA_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def reload_persona_config() -> dict:
    load_persona_config.cache_clear()
    return load_persona_config()


def _market_personas(market: str) -> list[dict]:
    return load_persona_config().get("personas", {}).get(market, []) or []


def persona_summary(persona: dict) -> dict:
    return {
        "id": persona["id"],
        "label": persona.get("label", persona["id"]),
        "assumption": persona.get("assumption"),
        "usage": persona.get("usage") or {},
    }


def list_personas(market: str) -> list[dict]:
    return [persona_summary(p) for p in _market_personas(market)]


def get_persona(market: str, persona_id: str | None) -> dict | None:
    """None -> no persona (market-wide ranking). Unknown id -> KeyError."""
    if persona_id is None:
        return None
    personas = _market_personas(market)
    for p in personas:
        if p["id"] == persona_id:
            return p
    raise KeyError(
        f"Unknown persona '{persona_id}' for market '{market}'. "
        f"Known: {[p['id'] for p in personas]}"
    )


def get_comparability_fields(market: str) -> list[str]:
    return list(load_persona_config().get("comparability", {}).get(market, []) or [])


def _compare(actual, op: str, required) -> bool:
    if op == "contains":
        needle = str(required).strip().lower()
        if isinstance(actual, list):
            return needle in [str(x).strip().lower() for x in actual]
        return needle in str(actual).lower()
    if op not in _OPS:
        raise ValueError(f"Unsupported operator '{op}'")
    if isinstance(actual, bool) or isinstance(required, bool):
        return op == "==" and bool(actual) == bool(required)
    if isinstance(actual, (int, float)) and isinstance(required, (int, float)):
        return _OPS[op](float(actual), float(required))
    if op == "==":
        return str(actual).strip().lower() == str(required).strip().lower()
    return False


def evaluate_persona(profile: dict, persona: dict) -> dict:
    """
    {"eligible": True|False|None,
     "checks": [{field, op, required, actual, passed, claim_id, status}],
     "missing": [fields that were not found]}
    """
    checks, missing = [], []
    for req in persona.get("requirements", []) or []:
        field, op, required = req["field"], req["op"], req["value"]
        fact = profile["facts"].get(field)
        actual = None if fact is None else fact["value"]

        if actual is None:
            missing.append(field)
            checks.append({
                "field": field, "op": op, "required": required, "actual": None,
                "passed": None, "claim_id": None, "status": "not found",
            })
            continue

        passed = _compare(actual, op, required)
        checks.append({
            "field": field, "op": op, "required": required, "actual": actual,
            "passed": passed, "claim_id": fact["claim_id"],
            "status": "met" if passed else "not met",
        })

    if any(c["passed"] is False for c in checks):
        eligible = False
    elif missing:
        eligible = None
    else:
        eligible = True
    return {"eligible": eligible, "checks": checks, "missing": missing}