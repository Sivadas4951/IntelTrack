"""
alert_router.py
---------------
Routes classified changes to notification channels.

  high / medium -> immediate alert (channels per severity tier, see notifications/channels.py)
  low           -> daily digest only
  duplicates    -> suppressed (same competitor + plan + field + change type within the window)

Fixes vs the previous version:
  * Dedup is now per competitor + plan (before, a change on one competitor
    silenced the same field on every other competitor).
  * The dedup time window compared ISO strings with SQLite's datetime(), which
    mismatches ('T' vs ' ') and could suppress alerts all day. The cutoff is
    now computed in Python in the same ISO format that is stored.
  * One alerts row is written per channel actually used.
"""

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from notifications.channels import send_alert

IMMEDIATE_THRESHOLD = {"high", "medium"}
DEDUP_WINDOW_MINUTES = 30


def route_alerts(changes: list[dict]):
    """Send alerts for classified changes. Called by pipeline.py."""
    for change in changes:
        if _is_duplicate(change):
            continue

        severity = change.get("severity", "low")
        if severity in IMMEDIATE_THRESHOLD:
            _send_console(change)
            delivered = send_alert(change)
            for ch in (delivered or ["console"]):
                _record_alert(change["id"], ch)
        else:
            _log_digest(change)
            _record_alert(change["id"], "digest")


def _is_duplicate(change: dict) -> bool:
    """Same competitor + plan + field + change type already alerted recently?"""
    diff = change.get("_diff", {})
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=DEDUP_WINDOW_MINUTES)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    count = conn.execute(
        """SELECT COUNT(*) FROM alerts a
           JOIN changes c ON a.change_id = c.id
           JOIN claims cl ON cl.claim_id = c.claim_id_new
           WHERE c.field = ? AND c.change_type = ?
             AND cl.competitor = ? AND cl.plan_or_sku = ?
             AND a.sent_at > ?""",
        (change["field"], change["change_type"],
         change.get("_competitor"), diff.get("plan_or_sku"), cutoff),
    ).fetchone()[0]
    conn.close()
    return count > 0


def _send_console(change: dict):
    diff = change.get("_diff", {})
    stealth_tag = " [STEALTH]" if change.get("is_stealth") else ""
    print(
        f"\n{'='*60}\n"
        f"ALERT [{change['severity'].upper()}]{stealth_tag}\n"
        f"   Competitor : {change.get('_competitor', '?')}\n"
        f"   Market     : {change.get('_market', '?')}\n"
        f"   Field      : {change['field']}\n"
        f"   Change     : {diff.get('old_value')} -> {diff.get('new_value')}\n"
        f"   Type       : {change['change_type']}\n"
        f"   Change ID  : {change['id']}\n"
        f"   Claim old  : {change.get('claim_id_old', '-')}\n"
        f"   Claim new  : {change.get('claim_id_new', '-')}\n"
        f"{'='*60}\n"
    )


def _log_digest(change: dict):
    diff = change.get("_diff", {})
    print(
        f"[DIGEST] {change.get('_competitor','?')} | "
        f"{change['field']}: {diff.get('old_value')} -> {diff.get('new_value')} "
        f"[{change['severity']}]"
    )


def _record_alert(change_id: str, channel: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO alerts (id, change_id, channel, sent_at, feedback) "
        "VALUES (?,?,?,?,?)",
        (f"alt_{uuid.uuid4().hex[:12]}", change_id, channel,
         datetime.now(timezone.utc).isoformat(), None),
    )
    conn.commit()
    conn.close()