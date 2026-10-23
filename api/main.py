"""
api/main.py
-----------
Person 4 backend: one FastAPI app that serves everything the dashboard needs.
It only reads/writes through the existing modules and tables, so the
"no claim without a snapshot, no sentence without a claim ID" rule is preserved.

Run from the project root (mock sites stay on 8001):
    uvicorn api.main:app --port 8000 --reload
Docs: http://localhost:8000/docs
"""

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

from core.db import DB_PATH, init_db
from analytics import claim_store, insights, live_analysis, report_store, watch_rules
from analytics.config_loader import get_market_rules, supported_markets
from analytics.personas import list_personas
from analytics.ranking import rank_market
from notifications.channels import configured_channels
from notifications.digest import build_digest, send_digest

from api.stage5 import router as stage5_router

app = FastAPI(title="IntelTrack API", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.include_router(stage5_router)  # stage 5 + value map

SCREENSHOT_DIR = os.path.abspath("data/screenshots")


@app.on_event("startup")
def _startup():
    init_db()
    live_analysis.ensure_tables()
    report_store.ensure_tables()
    watch_rules.ensure_tables()


# ── helpers ──────────────────────────────────────────────────────────────────

def _rows(sql: str, params: tuple = ()) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    out = [dict(r) for r in conn.execute(sql, params).fetchall()]
    conn.close()
    return out


def _check_market(market: str) -> str:
    if market not in supported_markets():
        raise HTTPException(404, f"Unknown market '{market}'. Use one of {supported_markets()}")
    return market


# ── meta ─────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    return {
        "status": "ok",
        "time": datetime.now(timezone.utc).isoformat(),
        "notification_channels": configured_channels(),
    }


@app.get("/api/markets")
def markets():
    return [
        {"market": m, "personas": list_personas(m),
         "metrics": get_market_rules(m).get("metrics", []),
         "primary_metric": get_market_rules(m).get("primary_metric"),
         "regions": [r["region"] for r in _rows("SELECT DISTINCT region FROM sources WHERE market = ?", (m,))]}
        for m in supported_markets()
    ]


# ── changes and alerts ───────────────────────────────────────────────────────

@app.get("/api/changes")
def changes(hours: int = 24, market: str | None = None,
            min_severity: Literal["low", "medium", "high"] | None = None,
            only_stealth: bool = False):
    if market:
        _check_market(market)
    return claim_store.get_recent_changes(hours=hours, market=market,
                                          min_severity=min_severity, only_stealth=only_stealth)


@app.get("/api/changes/{change_id}")
def change_detail(change_id: str):
    ch = claim_store.get_change(change_id)
    if not ch:
        raise HTTPException(404, "Change not found")
    return {"change": ch, "analysis": live_analysis.get_analysis(change_id)}


@app.get("/api/alerts")
def alerts(hours: int = 24, limit: int = Query(100, le=500)):
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return _rows(
        """SELECT a.id, a.change_id, a.channel, a.sent_at, a.feedback,
                  ch.field, ch.severity, ch.is_stealth, cl.competitor, cl.market
           FROM alerts a
           JOIN changes ch ON ch.id = a.change_id
           LEFT JOIN claims cl ON cl.claim_id = ch.claim_id_new
           WHERE a.sent_at >= ? ORDER BY a.sent_at DESC LIMIT ?""",
        (cutoff, limit),
    )


class Feedback(BaseModel):
    feedback: Literal["useful", "noise"]


@app.post("/api/alerts/{alert_id}/feedback")
def alert_feedback(alert_id: str, body: Feedback):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.execute("UPDATE alerts SET feedback = ? WHERE id = ?", (body.feedback, alert_id))
    conn.commit()
    conn.close()
    if cur.rowcount == 0:
        raise HTTPException(404, "Alert not found")
    return {"id": alert_id, "feedback": body.feedback}


@app.get("/api/digest")
def digest(hours: int = 24):
    return build_digest(hours)


@app.post("/api/digest/send")
def digest_send(hours: int = 24):
    return send_digest(hours)


# ── analytics (model lane unless the field says otherwise) ──────────────────

@app.get("/api/leaderboard")
def leaderboard(market: str, persona_id: str | None = None, rank_by: str | None = None,
                region: str | None = None, headline_only: bool = True):
    _check_market(market)
    try:
        return rank_market(market, persona_id=persona_id, rank_by=rank_by,
                           region=region, headline_only=headline_only)
    except KeyError as e:
        raise HTTPException(404, f"Unknown persona or metric: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.get("/api/metrics")
def metrics(market: str, metric: str | None = None, competitor: str | None = None):
    """Latest stored metrics (effective price, volatility, VIS ...). Each row has inputs_claim_ids."""
    _check_market(market)
    return claim_store.get_latest_metrics(market, metric=metric, competitor=competitor)


@app.get("/api/metrics/history")
def metric_history(market: str, competitor: str, metric_name: str, limit: int = 200):
    _check_market(market)
    return claim_store.get_metric_history(market, competitor, metric_name, limit)


@app.get("/api/history")
def history(market: str):
    """Price trend, change frequency and promo cadence per plan."""
    return insights.market_history(_check_market(market))


@app.get("/api/price-leaders")
def price_leaders(market: str):
    """Who moves first and who follows (labelled evidence level inside the result)."""
    return insights.response_model(_check_market(market))


@app.get("/api/timeline")
def timeline(market: str, competitor: str, field: str, plan_or_sku: str | None = None):
    _check_market(market)
    return claim_store.get_claim_history(market, competitor, field, plan_or_sku=plan_or_sku)


# ── evidence ─────────────────────────────────────────────────────────────────

@app.get("/api/claims/{claim_id}")
def claim(claim_id: str):
    c = claim_store.get_claim(claim_id)
    if not c:
        raise HTTPException(404, "Claim not found")
    return {"claim": c, "snapshot": claim_store.get_snapshot(c["snapshot_id"])}


@app.get("/api/snapshots/{snapshot_id}/screenshot")
def screenshot(snapshot_id: str):
    rows = _rows("SELECT screenshot_path FROM snapshots WHERE id = ?", (snapshot_id,))
    path = rows[0]["screenshot_path"] if rows else None
    if not path:
        raise HTTPException(404, "No screenshot for this snapshot")
    full = os.path.abspath(path)
    if not full.startswith(SCREENSHOT_DIR) or not os.path.exists(full):
        raise HTTPException(404, "Screenshot file missing")
    return FileResponse(full, media_type="image/png")


# ── Counter-Move cards (nothing is sent automatically) ──────────────────────

@app.get("/api/cards")
def cards(status: Literal["pending", "approved", "dismissed", "expired"] | None = None, limit: int = 100):
    claim_store.expire_stale_cards()
    return claim_store.get_cards(status=status, limit=limit)


def _set_card(card_id: str, status: str):
    if not claim_store.update_card_status(card_id, status):
        raise HTTPException(404, "Card not found")
    return claim_store.get_card(card_id)


@app.post("/api/cards/{card_id}/approve")
def card_approve(card_id: str):
    return _set_card(card_id, "approved")


@app.post("/api/cards/{card_id}/dismiss")
def card_dismiss(card_id: str):
    return _set_card(card_id, "dismissed")


# ── reports ──────────────────────────────────────────────────────────────────

@app.get("/api/reports")
def reports(market: str | None = None, report_type: str | None = None):
    return report_store.list_reports(market=market, report_type=report_type)


@app.get("/api/reports/{report_id}")
def report(report_id: str):
    r = report_store.get_report(report_id)
    if not r:
        raise HTTPException(404, "Report not found")
    return r


# ── watch rules (LLM/rule parses -> user confirms -> plain code checks) ─────

class RuleIn(BaseModel):
    text: str


@app.get("/api/watch-rules")
def rules_list():
    return watch_rules.list_rules()


@app.post("/api/watch-rules")
def rules_add(body: RuleIn):
    return watch_rules.add_rule(body.text)  # returns the parsed rule for the user to confirm


@app.post("/api/watch-rules/{rule_id}/confirm")
def rules_confirm(rule_id: str):
    if not watch_rules.confirm_rule(rule_id):
        raise HTTPException(404, "Rule not found")
    return {"id": rule_id, "confirmed": True}


@app.get("/api/watch-rules/hits")
def rules_hits(hours: int = 24 * 30):
    return watch_rules.check_rules(hours=hours)


# ── maintenance ──────────────────────────────────────────────────────────────

@app.post("/api/refresh")
def refresh():
    """Re-run live analysis on any new changes and expire stale cards."""
    analysed = live_analysis.process_pending_changes()
    expired = claim_store.expire_stale_cards()
    return {"changes_analysed": len(analysed), "cards_expired": expired}