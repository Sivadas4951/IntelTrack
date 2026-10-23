"""
api/stage5.py (sub-stage 5.1)
-----------------------------
Backend for the three stage-5 tabs. Mounted into the existing app with one line
(see api_patch/stage5_patch.md). Read-only: nothing here writes to the database.

  GET /api/value-map                   value map: feature score vs price with fair-value line
  GET /api/evaluation                  results written by `python evaluate.py`
  GET /api/compliance                  sources, robots.txt and rate-limit log, provenance counts
  GET /api/ledger/verify               recompute the snapshot hash chain (tamper evidence)
  GET /api/time-machine/snapshots      snapshots per source, for the date slider
  GET /api/time-machine/compare        changed / added / removed facts between two snapshots
"""

import difflib
import json
import os
import re
import sqlite3
from datetime import datetime
from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from analytics.config_loader import supported_markets
from analytics.value_map import build_value_map
from core.db import DB_PATH
from core.ledger import compute_hash

router = APIRouter(prefix="/api")

EVAL_PATH = os.path.abspath("data/eval_results.json")
AUDIT_LOG = os.path.abspath("data/compliance_audit.log")
MOCK_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0"}
MAX_TEXT = 20000
LOG_LINE = re.compile(r"^(\S+ \S+) \[(\w+)\] (\w+)\s*\|\s*(.*)$")


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


# ── Value map (4.3) ──────────────────────────────────────────────────────────

@router.get("/value-map")
def value_map(market: str, region: str | None = None):
    if market not in supported_markets():
        raise HTTPException(404, f"Unknown market '{market}'. Use one of {supported_markets()}")
    return build_value_map(market, region=region)


# ── Accuracy tab ─────────────────────────────────────────────────────────────

@router.get("/evaluation")
def evaluation():
    if not os.path.exists(EVAL_PATH):
        raise HTTPException(404, "No evaluation results yet. Run: python evaluate.py")
    with open(EVAL_PATH, encoding="utf-8") as f:
        data = json.load(f)
    data["file_modified"] = datetime.fromtimestamp(os.path.getmtime(EVAL_PATH)).isoformat()
    return data


# ── Compliance panel ─────────────────────────────────────────────────────────

def _parse_log(tail: int = 300) -> dict:
    entries, counts = [], {}
    allowed = disallowed = 0
    blocked_urls: list[str] = []
    if os.path.exists(AUDIT_LOG):
        with open(AUDIT_LOG, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = LOG_LINE.match(line.strip())
                if not m:
                    continue
                ts, level, event, rest = m.groups()
                counts[event] = counts.get(event, 0) + 1
                fields = {}
                for part in rest.split(" | "):
                    if ": " in part:
                        k, v = part.split(": ", 1)
                        fields[k.strip()] = v.strip()
                if event == "ROBOTS_CHECK":
                    if fields.get("Status") == "ALLOWED":
                        allowed += 1
                    else:
                        disallowed += 1
                        if fields.get("URL") and fields["URL"] not in blocked_urls:
                            blocked_urls.append(fields["URL"])
                entries.append({"time": ts, "level": level, "event": event,
                                "url": fields.get("URL") or fields.get("Domain"), "detail": rest})
    return {
        "exists": os.path.exists(AUDIT_LOG), "counts": counts,
        "robots_allowed": allowed, "robots_disallowed": disallowed,
        "blocked_urls": blocked_urls[:20], "total_entries": len(entries),
        "entries": entries[-tail:][::-1],
    }


@router.get("/compliance")
def compliance():
    sources = _rows(
        "SELECT s.id, s.market, s.competitor, s.url, s.page_type, s.region, s.cadence_minutes, "
        "s.health_score, COUNT(sn.id) AS snapshots, MAX(sn.captured_at) AS last_captured "
        "FROM sources s LEFT JOIN snapshots sn ON sn.source_id = s.id "
        "GROUP BY s.id ORDER BY s.market, s.competitor, s.page_type")
    origin_by_source: dict[str, dict] = {}
    for r in _rows("SELECT source_id, origin, COUNT(*) AS n FROM snapshots GROUP BY source_id, origin"):
        origin_by_source.setdefault(r["source_id"], {})[r["origin"]] = r["n"]
    for s in sources:
        host = (urlparse(s["url"]).hostname or "").lower()
        s["host"] = host
        s["site_kind"] = "mock (ours)" if host in MOCK_HOSTS else "real site"
        s["origins"] = origin_by_source.get(s["id"], {})

    policy = {"user_agent": None, "rate_limit_seconds": None}
    try:  # importing the guard also opens the audit log for logging; harmless here
        from collector.compliance import USER_AGENT, compliance_guard
        policy = {"user_agent": USER_AGENT, "rate_limit_seconds": compliance_guard.rate_limit_seconds}
    except Exception as e:  # keep the panel usable even if the collector cannot import
        policy["error"] = str(e)

    return {
        "policy": policy,
        "sources": sources,
        "origin_totals": {r["origin"]: r["n"] for r in _rows(
            "SELECT origin, COUNT(*) AS n FROM snapshots GROUP BY origin")},
        "log": _parse_log(),
    }


@router.get("/ledger/verify")
def ledger_verify(source_id: str | None = None):
    """
    Recomputes, for every snapshot:
      hash   sha256(source|captured_at|content_hash|prev) must equal the stored ledger_hash
      content sha256(raw_text + screenshot bytes) must equal content_hash (None if the file is gone)
      chain  a live snapshot's prev hash must be the ledger_hash of the snapshot before it
    """
    sql = ("SELECT id, source_id, captured_at, content_hash, prev_snapshot_hash, ledger_hash, "
           "raw_text, screenshot_path, origin FROM snapshots")
    params: tuple = ()
    if source_id:
        sql += " WHERE source_id = ?"
        params = (source_id,)
    rows = _rows(sql + " ORDER BY source_id, captured_at", params)

    res = {"checked": len(rows), "hash_ok": 0, "content_ok": 0, "content_unverifiable": 0,
           "chain_checked": 0, "chain_ok": 0, "issues": []}

    def issue(r, kind, msg):
        if len(res["issues"]) < 50:
            res["issues"].append({"snapshot_id": r["id"], "source_id": r["source_id"],
                                  "kind": kind, "detail": msg})

    prev_row = None
    for r in rows:
        if prev_row is not None and prev_row["source_id"] != r["source_id"]:
            prev_row = None

        expect = compute_hash(f"{r['source_id']}|{r['captured_at']}|{r['content_hash']}|{r['prev_snapshot_hash']}")
        if expect == r["ledger_hash"]:
            res["hash_ok"] += 1
        else:
            issue(r, "hash", "ledger hash does not match its inputs")

        data = (r["raw_text"] or "").encode("utf-8")
        shot = r["screenshot_path"]
        if shot and not os.path.exists(shot):
            res["content_unverifiable"] += 1
        else:
            if shot:
                with open(shot, "rb") as f:
                    data += f.read()
            if compute_hash(data) == r["content_hash"]:
                res["content_ok"] += 1
            else:
                issue(r, "content", "stored text/screenshot no longer matches the content hash")

        if r["origin"] == "live":
            res["chain_checked"] += 1
            want = prev_row["ledger_hash"] if prev_row else "GENESIS"
            if r["prev_snapshot_hash"] == want:
                res["chain_ok"] += 1
            else:
                issue(r, "chain", "previous-hash link does not point at the snapshot before it")
        prev_row = r

    res["all_ok"] = (res["hash_ok"] == res["checked"] and not res["issues"])
    return res


# ── Time Machine ─────────────────────────────────────────────────────────────

@router.get("/time-machine/snapshots")
def tm_snapshots(market: str, competitor: str | None = None):
    sql = ("SELECT sn.id AS snapshot_id, sn.source_id, sn.captured_at, sn.origin, "
           "(sn.screenshot_path IS NOT NULL AND sn.screenshot_path != '') AS has_screenshot, "
           "s.competitor, s.url, s.page_type, s.region "
           "FROM snapshots sn JOIN sources s ON s.id = sn.source_id WHERE s.market = ?")
    params: list = [market]
    if competitor:
        sql += " AND s.competitor = ?"
        params.append(competitor)
    return _rows(sql + " ORDER BY sn.source_id, sn.captured_at", tuple(params))


def _snapshot(snapshot_id: str) -> dict:
    rows = _rows("SELECT sn.id AS snapshot_id, sn.source_id, sn.captured_at, sn.origin, sn.raw_text, "
                 "sn.screenshot_path, s.competitor, s.url, s.page_type, s.region "
                 "FROM snapshots sn JOIN sources s ON s.id = sn.source_id WHERE sn.id = ?", (snapshot_id,))
    if not rows:
        raise HTTPException(404, f"Snapshot '{snapshot_id}' not found")
    r = rows[0]
    text = r.pop("raw_text") or ""
    r["raw_text"] = text[:MAX_TEXT]
    r["text_truncated"] = len(text) > MAX_TEXT
    r["has_screenshot"] = bool(r.pop("screenshot_path"))
    return r


def _facts(snapshot_id: str) -> dict:
    out = {}
    for c in _rows("SELECT claim_id, plan_or_sku, field, value, unit, region, ambiguity FROM claims "
                   "WHERE snapshot_id = ? AND verified = 1 ORDER BY claim_id", (snapshot_id,)):
        out[(c["plan_or_sku"], c["field"], c["region"], c["ambiguity"])] = c
    return out


def _sentences(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"(?<=[.!?])\s+|\n+", text) if p.strip()]


@router.get("/time-machine/compare")
def tm_compare(before: str, after: str):
    b, a = _snapshot(before), _snapshot(after)
    fb, fa = _facts(before), _facts(after)

    ids = [c["claim_id"] for c in fa.values()]
    change_by_claim = {}
    if ids:
        q = ",".join("?" * len(ids))
        for ch in _rows(f"SELECT id, claim_id_new, severity, is_stealth, change_type FROM changes "
                        f"WHERE claim_id_new IN ({q})", tuple(ids)):
            change_by_claim[ch["claim_id_new"]] = {
                "id": ch["id"], "severity": ch["severity"],
                "is_stealth": bool(ch["is_stealth"]), "change_type": ch["change_type"]}

    changed, added, removed, same = [], [], [], 0
    for key in sorted(set(fb) | set(fa), key=lambda k: tuple(str(x) for x in k)):
        o, n = fb.get(key), fa.get(key)
        base = {"plan_or_sku": key[0], "field": key[1], "region": key[2], "ambiguity": key[3]}
        if o and n:
            if str(o["value"]) == str(n["value"]):
                same += 1
            else:
                changed.append({**base, "unit": n["unit"] or o["unit"],
                                "old_value": o["value"], "new_value": n["value"],
                                "claim_id_old": o["claim_id"], "claim_id_new": n["claim_id"],
                                "change": change_by_claim.get(n["claim_id"])})
        elif n:
            added.append({**base, "unit": n["unit"], "new_value": n["value"], "claim_id_new": n["claim_id"]})
        else:
            removed.append({**base, "unit": o["unit"], "old_value": o["value"], "claim_id_old": o["claim_id"]})

    sb, sa = _sentences(b["raw_text"]), _sentences(a["raw_text"])
    sm = difflib.SequenceMatcher(None, sb, sa, autojunk=False)
    rem, add = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag in ("replace", "delete"):
            rem += sb[i1:i2]
        if tag in ("replace", "insert"):
            add += sa[j1:j2]

    return {
        "before": b, "after": a, "same_source": b["source_id"] == a["source_id"],
        "changed": changed, "added": added, "removed": removed, "unchanged_count": same,
        "text_diff": {"removed": rem[:60], "added": add[:60]},
    }