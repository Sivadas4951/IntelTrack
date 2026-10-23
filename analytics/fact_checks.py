"""
fact_checks.py
--------------
Deterministic number-and-citation checks shared by the report checker (Stage 4)
and the Counter-Move card checker (Stage 5). No LLM, no randomness.

A sentence passes only if
  1. it has a lane ("evidence" or "model"),
  2. it cites at least one claim ID,
  3. every cited claim exists and is verified,
  4. each citation (source URL, observation time, origin) matches the database,
  5. every number in its text is stated in a cited claim (evidence lane), or is
     stated in / recomputed from the cited claims (model lane).

Model-lane recomputation that is allowed:
  * the market metrics from value_rules.yaml (cost per GB, cost per litre, ...)
  * absolute change and percentage change between consecutive cited states
  * thresholds and usage figures of the persona named in sentence["persona_id"]
  * Stage 6b: the Value Integrity Score (P, Q, B, VIS) and the market's VIS
    weights, recomputed from the cited claims -- ONLY for a sentence that sets
    sentence["is_vis"] = True (same opt-in pattern as persona_id; without the
    flag a model sentence must not get VIS's weight constants added to its
    allowed numbers, or a coincidental match like 0.5 == isp's price weight
    would let a fabricated number slip through)
Nothing else. A number outside these sets is rejected.

Note: the OTT-price-field constant/slugify helper below duplicates two lines
from analytics.bundle_decomposition instead of importing it, because
bundle_decomposition imports FROM this module (importing it back here would
be a circular import). Keep the two in sync if the price-field naming ever
changes.
"""

import re

from analytics.claim_store import get_claims_by_ids, get_snapshot, parse_fact, parse_number
from analytics.config_loader import canonical_field, get_market_rules
from analytics.personas import get_persona
from analytics.value_functions import compute_metric, vis_components

LANES = ("evidence", "model")
EPS = 1e-9

_NUM_RE = re.compile(r"(?<!\d)(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")

_PRICE_FIELD_PREFIX = "ott_price_inr_per_month_"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _price_field(item: str) -> str:
    return _PRICE_FIELD_PREFIX + _SLUG_RE.sub("_", item.lower()).strip("_")


def _item_key(item: str) -> str:
    return " ".join(str(item).lower().split())


def _is_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool)


def extract_numbers(text) -> list[str]:
    return [m.group() for m in _NUM_RE.finditer(str(text))]


def _to_float(token: str) -> float:
    return float(token.replace(",", ""))


def _decimals(token: str) -> int:
    clean = token.replace(",", "")
    return len(clean.split(".")[1]) if "." in clean else 0


def _numbers_in(value) -> list[float]:
    if value is None:
        return []
    return [_to_float(t) for t in extract_numbers(value)]


def fmt_num(x) -> str:
    x = float(x)
    if abs(x - round(x)) < EPS:
        return str(int(round(x)))
    for d in (2, 3, 4):
        if abs(round(x, d) - x) < EPS:
            return f"{x:.{d}f}"
    return f"{x:.4f}"


def number_matches(token: str, allowed) -> bool:
    x = _to_float(token)
    d = _decimals(token)
    for a in allowed:
        a = abs(a)
        if abs(a - x) < EPS:
            return True
        if d >= 2 and abs(a - round(a)) > EPS and abs(round(a, d) - x) < EPS:
            return True
    return False


def _state_series(market: str, claims: list[dict]) -> list[dict]:
    ordered = sorted(claims, key=lambda c: (c["observed_at"], c["claim_id"]))
    times = sorted({c["observed_at"] for c in ordered})
    series = []
    for t in times:
        latest = {}
        for c in ordered:
            if c["observed_at"] <= t:
                latest[canonical_field(market, c["field"])] = c
        series.append({canon: parse_fact(market, canon, c["value"]) for canon, c in latest.items()})
    return series


def _add_deltas(allowed: set, a: dict, b: dict):
    for key in set(a) & set(b):
        x, y = a[key], b[key]
        if _is_num(x) and _is_num(y):
            allowed.add(abs(y - x))
            if abs(x) > EPS:
                allowed.add(abs((y - x) / x * 100.0))


def _add_persona_numbers(allowed: set, market: str, persona_id: str):
    try:
        persona = get_persona(market, persona_id)
    except KeyError:
        return
    if not persona:
        return
    for req in persona.get("requirements") or []:
        v = req.get("value")
        if _is_num(v):
            allowed.add(float(v))
    for v in (persona.get("usage") or {}).values():
        if _is_num(v):
            allowed.add(float(v))
    for key in ("label", "assumption"):
        allowed.update(_numbers_in(persona.get(key)))


def _latest_two(plan_claims: list[dict], market: str, canon: str):
    rows = sorted(
        (c for c in plan_claims
         if canonical_field(market, c["field"]) == canon and int(c.get("verified") or 0) == 1),
        key=lambda c: (c["observed_at"], c["claim_id"]),
    )
    if not rows:
        return None, None
    first_v = parse_fact(market, canon, rows[0]["value"])
    last_v = parse_fact(market, canon, rows[-1]["value"])
    return first_v, last_v


def _add_vis_numbers(allowed: set, market: str, plan_claims: list[dict]):
    try:
        rules = get_market_rules(market)
    except KeyError:
        return
    weights = rules.get("weights")
    if not weights:
        return

    price_before, price_after = _latest_two(plan_claims, market, "price")

    quantity_moves = []
    for field, ftype in rules.get("field_types", {}).items():
        if ftype != "number" or field == "price":
            continue
        if rules.get("field_direction", {}).get(field) != "higher_is_better":
            continue
        b, a = _latest_two(plan_claims, market, field)
        quantity_moves.append((field, b, a))

    old_list, new_list = _latest_two(plan_claims, market, "ott_bundle")
    bundle_dropped = []
    if isinstance(old_list, list) and isinstance(new_list, list):
        new_keys = {_item_key(x) for x in new_list}
        price_claims = {
            c["field"]: parse_number(c["value"]) for c in plan_claims
            if c["field"].startswith(_PRICE_FIELD_PREFIX) and int(c.get("verified") or 0) == 1
        }
        for item in old_list:
            if _item_key(item) in new_keys:
                continue
            price = price_claims.get(_price_field(item))
            if price is not None and price > 0:
                bundle_dropped.append({"item": item, "status": "quantified", "price_inr_per_month": price})
            else:
                bundle_dropped.append({"item": item, "status": "unquantified value loss",
                                        "price_inr_per_month": None})

    comp = vis_components(weights, price_before, price_after, quantity_moves, bundle_dropped)
    for v in (comp["p"], comp["q"], comp["b"], comp["vis"]):
        allowed.add(v)
    for w in weights.values():
        if _is_num(w):
            allowed.add(float(w))
    allowed.add(100.0)


def allowed_numbers(claims: list[dict], persona_id: str | None = None, include_derived: bool = True,
                     is_vis: bool = False) -> set:
    allowed: set = set()
    per_plan: dict[tuple, list[dict]] = {}
    for c in claims:
        for field in ("value", "plan_or_sku", "competitor"):
            allowed.update(_numbers_in(c.get(field)))
        per_plan.setdefault((c["market"], c["competitor"], c["plan_or_sku"]), []).append(c)

    if not include_derived:
        return allowed

    for (market, _comp, _plan), plan_claims in per_plan.items():
        try:
            rules = get_market_rules(market)
        except KeyError:
            continue
        series = _state_series(market, plan_claims)
        metric_series = []
        for values in series:
            metrics = {}
            for metric in rules.get("metrics", []):
                v = compute_metric(market, metric, values, rules)
                if v is not None:
                    metrics[metric] = v
                    allowed.add(v)
            metric_series.append(metrics)
        for i in range(len(series) - 1):
            _add_deltas(allowed, series[i], series[i + 1])
            _add_deltas(allowed, metric_series[i], metric_series[i + 1])
        if persona_id:
            _add_persona_numbers(allowed, market, persona_id)
        if is_vis:
            _add_vis_numbers(allowed, market, plan_claims)
    return allowed


def claim_origin(claim: dict) -> str:
    snap = get_snapshot(claim["snapshot_id"])
    return snap["origin"] if snap else "unknown"


def clean_claim_ids(ids) -> list[str]:
    if not isinstance(ids, list):
        return []
    return list(dict.fromkeys(i.strip() for i in ids if isinstance(i, str) and i.strip()))


def build_citations(claim_ids: list[str]) -> list[dict]:
    ids = clean_claim_ids(claim_ids)
    found = get_claims_by_ids(ids)
    out = []
    for cid in ids:
        c = found.get(cid)
        if c:
            out.append({
                "claim_id": cid, "source_url": c["source_url"], "observed_at": c["observed_at"],
                "origin": claim_origin(c), "snapshot_id": c["snapshot_id"],
                "field": c["field"], "value": c["value"],
            })
    return out


def _issue(code: str, message: str) -> dict:
    return {"code": code, "message": message}


def _citation_issues(sentence: dict, ids: list[str], found: dict) -> list[dict]:
    by_id = {}
    cits = sentence.get("citations")
    if isinstance(cits, list):
        for c in cits:
            if isinstance(c, dict) and c.get("claim_id"):
                by_id[c["claim_id"]] = c
    issues = []
    for cid in ids:
        claim = found.get(cid)
        if claim is None:
            continue
        cit = by_id.get(cid)
        if cit is None:
            issues.append(_issue("citation_missing", f"No source citation attached for {cid}."))
            continue
        expected = {
            "source_url": claim["source_url"],
            "observed_at": claim["observed_at"],
            "origin": claim_origin(claim),
        }
        for key, want in expected.items():
            if cit.get(key) != want:
                issues.append(_issue(
                    "citation_mismatch",
                    f"{cid}: citation {key} is {cit.get(key)!r} but the database says {want!r}.",
                ))
    return issues


def check_sentence(sentence: dict) -> list[dict]:
    if not isinstance(sentence, dict):
        return [_issue("bad_sentence", "Sentence is not an object.")]
    text = sentence.get("text")
    if not isinstance(text, str) or not text.strip():
        return [_issue("empty_text", "Sentence text is empty.")]

    issues = []
    lane = sentence.get("lane")
    if lane not in LANES:
        issues.append(_issue("bad_lane", f"Lane must be one of {LANES}, got {lane!r}."))

    ids = clean_claim_ids(sentence.get("claim_ids"))
    if not ids:
        issues.append(_issue("no_claim_id", "Sentence cites no claim ID."))
        return issues

    found = get_claims_by_ids(ids)
    for cid in ids:
        claim = found.get(cid)
        if claim is None:
            issues.append(_issue("unknown_claim", f"{cid} does not exist in the claims table."))
        elif int(claim.get("verified") or 0) != 1:
            issues.append(_issue("unverified_claim", f"{cid} is not a verified claim."))

    issues += _citation_issues(sentence, ids, found)

    allowed = allowed_numbers(
        [found[i] for i in ids if i in found],
        persona_id=sentence.get("persona_id"),
        include_derived=(lane == "model"),
        is_vis=bool(sentence.get("is_vis")),
    )
    for token in extract_numbers(text):
        if not number_matches(token, allowed):
            hint = (" Derived numbers belong in the model lane." if lane == "evidence" else "")
            issues.append(_issue(
                "number_not_in_facts",
                f"Number '{token}' is not stated in, or recomputable from, the cited claims.{hint}",
            ))
    return issues
