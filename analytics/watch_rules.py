"""
watch_rules.py
--------------
Stage 7: plain-English watch rules.
  parse_rule(text)  -> rule dict (or an error)     [swap in an LLM here later if wanted]
  add_rule / confirm_rule / list_rules             user must CONFIRM before a rule is live
  check_rules(hours)                               plain code, no LLM, checks confirmed rules

Run from the project root:
    python -m analytics.watch_rules --add "Alert me if SpeedFiber FUP cap drops below 2000"
    python -m analytics.watch_rules --list
    python -m analytics.watch_rules --confirm <rule_id>
    python -m analytics.watch_rules --check
    python -m analytics.watch_rules --selftest-only
"""

import argparse
import json
import re
import sqlite3
import sys
import uuid

from core.db import DB_PATH
from analytics.claim_store import get_change, get_recent_changes, now_iso, parse_list, parse_number
from analytics.config_loader import get_market_rules, supported_markets

FIELD_WORDS = [
    ("post_fup_speed_mbps", ["post-fup", "post fup"]),
    ("fup_cap_gb", ["fup", "data cap", "cap"]),
    ("pack_size_ml", ["pack size", "pack"]),
    ("fat_percentage", ["fat"]),
    ("battery_hours", ["battery"]),
    ("anc_enabled", ["anc", "noise cancel"]),
    ("warranty_months", ["warranty"]),
    ("ott_bundle", ["ott", "bundle"]),
    ("speed_mbps", ["speed"]),
    ("price", ["price", "pricing", "cost"]),
]
MARKET_WORDS = {
    "isp": ["isp", "broadband", "plan", "mbps", "fup", "ott", "bundle", "speed"],
    "dairy": ["milk", "dairy", "pack", "fat"],
    "headphones": ["headphone", "earbud", "battery", "anc", "warranty"],
}
DOWN = r"drop|fall|cut|reduc|decreas|remov|lower|shrink"
UP = r"rise|increas|hike|raise|go up|goes up"


# ── Storage ──────────────────────────────────────────────────────────────────

def ensure_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS p3_watch_rules (
        id TEXT PRIMARY KEY, text TEXT NOT NULL, rule_json TEXT NOT NULL,
        confirmed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL)""")
    conn.commit()
    conn.close()


def _known_competitors() -> dict[str, str]:
    conn = sqlite3.connect(DB_PATH)
    rows = conn.execute("SELECT DISTINCT competitor, market FROM claims").fetchall()
    conn.close()
    return {r[0]: r[1] for r in rows}


# ── Parser ───────────────────────────────────────────────────────────────────

def _has(word: str, text: str) -> bool:
    return re.search(r"\b" + re.escape(word), text) is not None


def parse_rule(text: str) -> dict:
    t = text.lower().replace(",", "")
    comps = _known_competitors()
    competitor, market = None, None
    for name, mkt in comps.items():
        if name.lower() in t:
            competitor, market = name, mkt
            break

    field = None
    for canon, words in FIELD_WORDS:
        if any(_has(w, t) for w in words):
            if market is None or canon in get_market_rules(market).get("field_map", {}):
                field = canon
                break
    if field is None:
        return {"ok": False, "error": "Could not tell which field to watch (price, speed, FUP cap, bundle, pack size, battery...)."}

    if market is None:
        owners = [m for m in supported_markets() if field in get_market_rules(m).get("field_map", {})]
        if len(owners) == 1:
            market = owners[0]
        else:
            scores = {m: sum(1 for w in MARKET_WORDS.get(m, []) if _has(w, t)) for m in owners}
            top = max(scores.values()) if scores else 0
            best = [m for m, s in scores.items() if s == top]
            if top == 0 or len(best) != 1:
                return {"ok": False, "error": "Which market is this for (isp, dairy or headphones)? Mention a competitor or the market."}
            market = best[0]

    pct = re.search(r"(\d+(?:\.\d+)?)\s*%", t)
    t_nopct = re.sub(r"(\d+(?:\.\d+)?)\s*%", " ", t)
    num = re.search(r"\d+(?:\.\d+)?", t_nopct)
    threshold, op = None, "changes"
    if pct and re.search(DOWN, t):
        op, threshold = "drops_by_pct", float(pct.group(1))
    elif pct and re.search(UP, t):
        op, threshold = "rises_by_pct", float(pct.group(1))
    elif pct:
        op, threshold = "changes_by_pct", float(pct.group(1))
    elif num and re.search(r"below|under|less than|lower than|drops to|falls to", t):
        op, threshold = "below", float(num.group())
    elif num and re.search(r"above|over|exceed|more than|higher than|greater than|rises to", t):
        op, threshold = "above", float(num.group())
    elif re.search(DOWN, t):
        op = "decreases"
    elif re.search(UP, t):
        op = "increases"

    return {"ok": True, "rule": {"market": market, "competitor": competitor, "field": field,
                                 "op": op, "threshold": threshold, "source_text": text}}


def add_rule(text: str) -> dict:
    ensure_tables()
    parsed = parse_rule(text)
    if not parsed["ok"]:
        return parsed
    rule_id = f"rule_{uuid.uuid4().hex[:8]}"
    conn = sqlite3.connect(DB_PATH)
    conn.execute("INSERT INTO p3_watch_rules (id, text, rule_json, confirmed, created_at) VALUES (?,?,?,0,?)",
                 (rule_id, text, json.dumps(parsed["rule"]), now_iso()))
    conn.commit()
    conn.close()
    parsed["id"] = rule_id
    return parsed


def confirm_rule(rule_id: str) -> bool:
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE p3_watch_rules SET confirmed = 1 WHERE id = ?", (rule_id,))
    conn.commit()
    conn.close()
    return cur.rowcount > 0


def list_rules(confirmed_only: bool = False) -> list[dict]:
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    sql = "SELECT * FROM p3_watch_rules" + (" WHERE confirmed = 1" if confirmed_only else "") + " ORDER BY created_at"
    rows = conn.execute(sql).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["rule"] = json.loads(d["rule_json"])
        out.append(d)
    return out


# ── Checker (plain code, no LLM) ─────────────────────────────────────────────

def _as_number(market: str, field: str, raw):
    ftype = get_market_rules(market).get("field_types", {}).get(field)
    if ftype == "list":
        return float(len(parse_list(raw)))
    if ftype == "bool":
        return 1.0 if str(raw).strip().lower() in ("true", "1", "yes") else 0.0
    return parse_number(raw)


def evaluate(rule: dict, change: dict) -> dict | None:
    if change["market"] != rule["market"] or change["field_canonical"] != rule["field"]:
        return None
    if rule.get("competitor") and change["competitor"] != rule["competitor"]:
        return None
    old = _as_number(rule["market"], rule["field"], change["old_value"])
    new = _as_number(rule["market"], rule["field"], change["new_value"])
    op, thr = rule["op"], rule.get("threshold")
    hit = False
    if op == "changes":
        hit = str(change["old_value"]) != str(change["new_value"])
    elif new is None:
        hit = False
    elif op == "below":
        hit = new < thr
    elif op == "above":
        hit = new > thr
    elif op == "decreases":
        hit = old is not None and new < old
    elif op == "increases":
        hit = old is not None and new > old
    elif old is not None and old > 0:
        pct = (new - old) / old * 100.0
        hit = ((op == "drops_by_pct" and -pct >= thr) or (op == "rises_by_pct" and pct >= thr)
               or (op == "changes_by_pct" and abs(pct) >= thr))
    if not hit:
        return None
    return {
        "change_id": change["change_id"], "competitor": change["competitor"],
        "plan_or_sku": change["plan_or_sku"], "field": rule["field"],
        "old_value": change["old_value"], "new_value": change["new_value"],
        "claim_ids": [i for i in (change.get("claim_id_old"), change.get("claim_id_new")) if i],
        "message": f"{change['competitor']} {change['plan_or_sku']}: {rule['field']} "
                   f"{change['old_value']} -> {change['new_value']} (rule: {rule['source_text']})",
    }


def check_rules(hours: int = 24 * 30) -> list[dict]:
    rules = list_rules(confirmed_only=True)
    changes = get_recent_changes(hours=hours)
    hits = []
    for r in rules:
        for c in changes:
            h = evaluate(r["rule"], c)
            if h:
                h["rule_id"] = r["id"]
                hits.append(h)
    return hits


# ── Self-tests (read-only: they never write rules) ───────────────────────────

def run_selftests() -> bool:
    good, total = 0, 0

    def record(name, behaved, outcome):
        nonlocal good, total
        total += 1
        good += 1 if behaved else 0
        print(f"  [{'ok' if behaved else 'WRONG'}] {name}: {outcome}")

    def parsed(text):
        p = parse_rule(text)
        return p["rule"] if p["ok"] else None

    r = parsed("Alert me if SpeedFiber FUP cap drops below 2000")
    record("parse: FUP cap below 2000",
           bool(r) and (r["market"], r["competitor"], r["field"], r["op"], r["threshold"])
           == ("isp", "SpeedFiber", "fup_cap_gb", "below", 2000.0), str(r))

    r = parsed("Tell me when FreshDairy pack size drops by more than 5%")
    record("parse: pack size drops by 5%",
           bool(r) and (r["market"], r["field"], r["op"], r["threshold"])
           == ("dairy", "pack_size_ml", "drops_by_pct", 5.0), str(r))

    r = parsed("any OTT bundle cut at NetStream")
    record("parse: bundle cut", bool(r) and (r["field"], r["op"], r["competitor"])
           == ("ott_bundle", "decreases", "NetStream"), str(r))

    r = parsed("notify me of battery changes")
    record("parse: battery changes -> headphones", bool(r) and (r["market"], r["op"])
           == ("headphones", "changes"), str(r))

    p = parse_rule("watch the price")
    record("ambiguous market is refused, not guessed", not p["ok"], p.get("error", ""))

    ch = get_change("SIM-chg-isp-speedfiber-fup_cap_gb")
    if ch is None:
        record("seeded SpeedFiber change exists", False, "missing (run the seed scripts)")
    else:
        base = {"market": "isp", "competitor": "SpeedFiber", "field": "fup_cap_gb", "source_text": "t"}
        record("FUP 3300->1500 triggers 'below 2000'",
               evaluate({**base, "op": "below", "threshold": 2000.0}, ch) is not None, "triggered")
        record("FUP 3300->1500 does NOT trigger 'below 1000'",
               evaluate({**base, "op": "below", "threshold": 1000.0}, ch) is None, "not triggered")
        record("FUP drop of 54.5% triggers 'drops by 50%'",
               evaluate({**base, "op": "drops_by_pct", "threshold": 50.0}, ch) is not None, "triggered")
        record("other competitor's rule does not trigger",
               evaluate({**base, "competitor": "AirWave", "op": "decreases", "threshold": None}, ch) is None,
               "not triggered")

    nb = get_change("SIM-chg-isp-netstream-ott_bundle")
    if nb is None:
        record("seeded NetStream bundle change exists", False, "missing (run seed_bundle_demo)")
    else:
        rule = {"market": "isp", "competitor": "NetStream", "field": "ott_bundle",
                "op": "decreases", "threshold": None, "source_text": "t"}
        record("bundle 2 items -> 1 triggers 'decreases'", evaluate(rule, nb) is not None, "triggered")

    print(f"Self-tests: {good}/{total} behaved as expected.")
    return good == total


# ── CLI ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 7 watch rules")
    parser.add_argument("--add", metavar="TEXT")
    parser.add_argument("--confirm", metavar="RULE_ID")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--hours", type=int, default=24 * 30)
    parser.add_argument("--selftest-only", action="store_true")
    args = parser.parse_args()

    if args.selftest_only:
        print("\n== watch rule self-tests ==")
        sys.exit(0 if run_selftests() else 1)
    elif args.add:
        res = add_rule(args.add)
        if not res["ok"]:
            print("Could not parse:", res["error"])
        else:
            print("Parsed rule (NOT active until you confirm):")
            print(json.dumps(res["rule"], indent=2))
            print(f"Confirm with: python -m analytics.watch_rules --confirm {res['id']}")
    elif args.confirm:
        print("Confirmed." if confirm_rule(args.confirm) else "No such rule.")
    elif args.list:
        for r in list_rules():
            print(f"{r['id']} [{'ACTIVE' if r['confirmed'] else 'unconfirmed'}] {r['text']}")
    elif args.check:
        hits = check_rules(args.hours)
        print("No watch rule triggered." if not hits else f"{len(hits)} watch rule hit(s):")
        for h in hits:
            print(f"  [{h['rule_id']}] {h['message']}  claims={h['claim_ids']}")
    else:
        parser.print_help()