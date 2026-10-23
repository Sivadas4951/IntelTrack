"""
claim_store.py
--------------
Data-access layer for Person 3 (reads claims/changes, writes metrics/cards).

Conventions
  * metrics.metric_name  = "<metric>@<plan_or_sku>"  e.g. "cost_per_mbps@Fiber Ultra"
    (the metrics table has no plan column, so the plan is encoded in the name)
  * metrics.inputs_claim_ids = JSON array string of claim IDs used
  * cards.body_json = JSON string of the card body
  * claims.value is TEXT: numbers like "799", lists like "A, B", booleans "true"/"false"
"""

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from analytics.config_loader import (
    canonical_field,
    get_headline_rules,
    get_market_rules,
    raw_field_names,
)

SEVERITY_RANK = {"low": 1, "medium": 2, "high": 3}
CARD_STATUSES = {"pending", "approved", "dismissed", "expired"}

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


# ── Connection helper ────────────────────────────────────────────────────────

@contextmanager
def _conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Value parsing ────────────────────────────────────────────────────────────

def parse_number(value):
    """'₹ 1,999' -> 1999.0, 'Up to 40 Hours' -> 40.0, None/'abc' -> None."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    match = _NUM_RE.search(str(value).replace(",", ""))
    return float(match.group()) if match else None


def parse_bool(value):
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    text = str(value).strip().lower()
    if text in ("true", "yes", "enabled", "included", "1"):
        return True
    if text in ("false", "no", "disabled", "not included", "0"):
        return False
    return None


def parse_list(value) -> list[str]:
    if value is None:
        return []
    return [p.strip() for p in re.split(r"[,;|]", str(value)) if p.strip()]


def parse_fact(market: str, canonical: str, raw_value):
    """Parse a raw claim value using the field type declared in value_rules.yaml."""
    ftype = get_market_rules(market).get("field_types", {}).get(canonical, "text")
    if ftype == "number":
        return parse_number(raw_value)
    if ftype == "bool":
        return parse_bool(raw_value)
    if ftype == "list":
        return parse_list(raw_value)
    return None if raw_value is None else str(raw_value).strip()


# ── Claims ───────────────────────────────────────────────────────────────────

def get_latest_claims(
    market: str,
    competitor: str | None = None,
    region: str | None = None,
    verified_only: bool = True,
    min_confidence: float = 0.0,
    exclude_ambiguity: list[str] | None = None,
    origins: list[str] | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """
    Latest claim per (competitor, plan_or_sku, field).
    'as_of' (ISO string) gives the state of the market at that time (Time Machine).
    """
    sql = [
        "SELECT c.*, s.origin AS origin FROM claims c "
        "LEFT JOIN snapshots s ON s.id = c.snapshot_id WHERE c.market = ?"
    ]
    params: list = [market]

    if competitor:
        sql.append("AND c.competitor = ?")
        params.append(competitor)
    if region:
        sql.append("AND c.region = ?")
        params.append(region)
    if verified_only:
        sql.append("AND c.verified = 1")
    if min_confidence:
        sql.append("AND c.claim_confidence >= ?")
        params.append(min_confidence)
    if exclude_ambiguity:
        sql.append(f"AND c.ambiguity NOT IN ({','.join('?' * len(exclude_ambiguity))})")
        params.extend(exclude_ambiguity)
    if origins:
        sql.append(f"AND s.origin IN ({','.join('?' * len(origins))})")
        params.extend(origins)
    if as_of:
        sql.append("AND c.observed_at <= ?")
        params.append(as_of)
    sql.append("ORDER BY c.observed_at ASC, c.claim_id ASC")

    with _conn() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()

    latest: dict[tuple, dict] = {}
    for row in rows:
        c = dict(row)
        c["field_canonical"] = canonical_field(market, c["field"])
        latest[(c["competitor"], c["plan_or_sku"], c["field_canonical"])] = c
    return list(latest.values())


def get_profiles(
    market: str,
    competitor: str | None = None,
    region: str | None = None,
    headline_only: bool = False,
    origins: list[str] | None = None,
    as_of: str | None = None,
) -> list[dict]:
    """
    One profile per (competitor, plan_or_sku):
      {"market","competitor","plan_or_sku","region","claim_ids":[...],
       "facts": {canonical: {"value" (parsed), "raw", "unit", "claim_id", "field",
                             "observed_at", "source_url", "confidence",
                             "ambiguity", "origin", "snapshot_id"}}}
    headline_only=True applies the confidence/ambiguity rules from value_rules.yaml.
    """
    kwargs = {}
    if headline_only:
        rules = get_headline_rules()
        kwargs = {
            "min_confidence": rules["min_confidence"],
            "exclude_ambiguity": rules["excluded_ambiguity"],
        }

    claims = get_latest_claims(
        market, competitor=competitor, region=region,
        origins=origins, as_of=as_of, **kwargs,
    )

    profiles: dict[tuple, dict] = {}
    for c in claims:
        key = (c["competitor"], c["plan_or_sku"])
        p = profiles.setdefault(key, {
            "market": market,
            "competitor": c["competitor"],
            "plan_or_sku": c["plan_or_sku"],
            "region": c["region"],
            "facts": {},
            "claim_ids": [],
        })
        canon = c["field_canonical"]
        p["facts"][canon] = {
            "value": parse_fact(market, canon, c["value"]),
            "raw": c["value"],
            "unit": c["unit"],
            "claim_id": c["claim_id"],
            "field": c["field"],
            "observed_at": c["observed_at"],
            "source_url": c["source_url"],
            "confidence": c["claim_confidence"],
            "ambiguity": c["ambiguity"],
            "origin": c.get("origin"),
            "snapshot_id": c["snapshot_id"],
        }
        p["claim_ids"].append(c["claim_id"])

    return sorted(profiles.values(), key=lambda p: (p["competitor"], p["plan_or_sku"]))


def fact_value(profile: dict, canonical: str):
    """Parsed value of a fact, or None if the profile does not have it ('not found')."""
    fact = profile.get("facts", {}).get(canonical)
    return None if fact is None else fact["value"]


def get_claim(claim_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM claims WHERE claim_id = ?", (claim_id,)).fetchone()
    return dict(row) if row else None


def get_claims_by_ids(claim_ids: list[str]) -> dict[str, dict]:
    ids = [i for i in claim_ids if i]
    if not ids:
        return {}
    with _conn() as conn:
        rows = conn.execute(
            f"SELECT * FROM claims WHERE claim_id IN ({','.join('?' * len(ids))})", ids
        ).fetchall()
    return {r["claim_id"]: dict(r) for r in rows}


def get_claim_history(
    market: str,
    competitor: str,
    field: str,
    plan_or_sku: str | None = None,
    verified_only: bool = True,
) -> list[dict]:
    """Every observed value of one field over time (oldest first). 'field' may be canonical."""
    names = raw_field_names(market, field)
    sql = (
        "SELECT c.*, s.origin AS origin FROM claims c "
        "LEFT JOIN snapshots s ON s.id = c.snapshot_id "
        f"WHERE c.market = ? AND c.competitor = ? AND c.field IN ({','.join('?' * len(names))})"
    )
    params: list = [market, competitor, *names]
    if plan_or_sku:
        sql += " AND c.plan_or_sku = ?"
        params.append(plan_or_sku)
    if verified_only:
        sql += " AND c.verified = 1"
    sql += " ORDER BY c.observed_at ASC, c.claim_id ASC"

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_snapshot(snapshot_id: str) -> dict | None:
    """Evidence info for a claim's snapshot (no raw text, kept small)."""
    with _conn() as conn:
        row = conn.execute(
            "SELECT id, source_id, captured_at, content_hash, ledger_hash, origin, screenshot_path "
            "FROM snapshots WHERE id = ?", (snapshot_id,)
        ).fetchone()
    return dict(row) if row else None


# ── Changes ──────────────────────────────────────────────────────────────────

_CHANGE_SELECT = """
SELECT ch.id AS change_id, ch.claim_id_old, ch.claim_id_new, ch.field,
       ch.change_type, ch.severity, ch.is_stealth, ch.detected_at, ch.status,
       cn.market AS market, cn.competitor AS competitor,
       cn.plan_or_sku AS plan_or_sku, cn.region AS region,
       cn.value AS new_value, cn.unit AS unit,
       cn.observed_at AS new_observed_at, cn.source_url AS source_url,
       cn.claim_confidence AS new_confidence,
       co.value AS old_value, co.observed_at AS old_observed_at
FROM changes ch
JOIN claims cn ON cn.claim_id = ch.claim_id_new
LEFT JOIN claims co ON co.claim_id = ch.claim_id_old
"""


def _change_row(row) -> dict:
    d = dict(row)
    d["field_canonical"] = canonical_field(d["market"], d["field"])
    return d


def get_change(change_id: str) -> dict | None:
    """A change with market/competitor/old+new values joined from claims.
    Returns None if its claims are missing (e.g. evaluate.py test rows)."""
    with _conn() as conn:
        row = conn.execute(_CHANGE_SELECT + " WHERE ch.id = ?", (change_id,)).fetchone()
    return _change_row(row) if row else None


def get_recent_changes(
    hours: int = 24,
    market: str | None = None,
    min_severity: str | None = None,
    only_stealth: bool = False,
) -> list[dict]:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    sql = _CHANGE_SELECT + " WHERE ch.detected_at >= ?"
    params: list = [cutoff]
    if market:
        sql += " AND cn.market = ?"
        params.append(market)
    if only_stealth:
        sql += " AND ch.is_stealth = 1"
    sql += " ORDER BY ch.detected_at DESC"

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    changes = [_change_row(r) for r in rows]
    if min_severity:
        floor = SEVERITY_RANK.get(min_severity, 1)
        changes = [c for c in changes if SEVERITY_RANK.get(c["severity"], 1) >= floor]
    return changes


# ── Metrics ──────────────────────────────────────────────────────────────────

def make_metric_name(metric: str, plan_or_sku: str) -> str:
    return f"{metric}@{plan_or_sku}"


def split_metric_name(metric_name: str) -> tuple[str, str]:
    metric, _, plan = metric_name.partition("@")
    return metric, plan


def save_metric(
    market: str,
    competitor: str,
    metric_name: str,
    value: float | None,
    inputs_claim_ids: list[str],
    computed_at: str | None = None,
) -> str | None:
    """Insert a metric row (history is kept). Returns the id, or None if value is None."""
    if value is None:
        return None
    metric_id = f"met_{uuid.uuid4().hex[:12]}"
    with _conn() as conn:
        conn.execute(
            "INSERT INTO metrics (id, market, competitor, metric_name, value, computed_at, inputs_claim_ids) "
            "VALUES (?,?,?,?,?,?,?)",
            (metric_id, market, competitor, metric_name, float(value),
             computed_at or now_iso(), json.dumps(inputs_claim_ids)),
        )
    return metric_id


def _metric_dict(row) -> dict:
    d = dict(row)
    try:
        d["inputs_claim_ids"] = json.loads(d["inputs_claim_ids"])
    except (TypeError, ValueError):
        d["inputs_claim_ids"] = []
    d["metric"], d["plan_or_sku"] = split_metric_name(d["metric_name"])
    return d


def get_latest_metrics(
    market: str,
    metric: str | None = None,
    competitor: str | None = None,
) -> list[dict]:
    """Latest row per (competitor, metric_name). 'metric' is the base name without @plan."""
    sql = "SELECT * FROM metrics WHERE market = ?"
    params: list = [market]
    if competitor:
        sql += " AND competitor = ?"
        params.append(competitor)
    sql += " ORDER BY computed_at ASC, id ASC"

    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()

    latest: dict[tuple, dict] = {}
    for r in rows:
        d = _metric_dict(r)
        if metric and d["metric"] != metric:
            continue
        latest[(d["competitor"], d["metric_name"])] = d
    return list(latest.values())


def get_metric_history(market: str, competitor: str, metric_name: str, limit: int = 200) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM metrics WHERE market = ? AND competitor = ? AND metric_name = ? "
            "ORDER BY computed_at ASC LIMIT ?",
            (market, competitor, metric_name, limit),
        ).fetchall()
    return [_metric_dict(r) for r in rows]


# ── Cards ────────────────────────────────────────────────────────────────────

def save_card(
    change_id: str,
    body: dict,
    owner: str,
    expires_at: str,
    status: str = "pending",
) -> str:
    if status not in CARD_STATUSES:
        raise ValueError(f"Invalid card status '{status}'")
    card_id = f"card_{uuid.uuid4().hex[:12]}"
    with _conn() as conn:
        conn.execute(
            "INSERT INTO cards (id, change_id, body_json, owner, expires_at, status) VALUES (?,?,?,?,?,?)",
            (card_id, change_id, json.dumps(body), owner, expires_at, status),
        )
    return card_id


def _card_dict(row) -> dict:
    d = dict(row)
    try:
        d["body"] = json.loads(d["body_json"])
    except (TypeError, ValueError):
        d["body"] = {}
    return d


def get_card(card_id: str) -> dict | None:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM cards WHERE id = ?", (card_id,)).fetchone()
    return _card_dict(row) if row else None


def get_cards(status: str | None = None, change_id: str | None = None, limit: int = 100) -> list[dict]:
    sql = "SELECT * FROM cards WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status = ?"
        params.append(status)
    if change_id:
        sql += " AND change_id = ?"
        params.append(change_id)
    sql += " ORDER BY expires_at ASC LIMIT ?"
    params.append(limit)
    with _conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_card_dict(r) for r in rows]


def update_card_status(card_id: str, status: str) -> bool:
    if status not in CARD_STATUSES:
        raise ValueError(f"Invalid card status '{status}'")
    with _conn() as conn:
        cur = conn.execute("UPDATE cards SET status = ? WHERE id = ?", (status, card_id))
        return cur.rowcount > 0


def expire_stale_cards() -> int:
    """Mark pending cards past their expiry date as 'expired'. Returns how many."""
    with _conn() as conn:
        cur = conn.execute(
            "UPDATE cards SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
            (now_iso(),),
        )
        return cur.rowcount