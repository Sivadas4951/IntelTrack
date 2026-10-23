"""
alert_router.py
---------------
Routes classified changes to notification channels.
Applies dedup, severity filtering and daily-digest logic.

Channels supported: console (always), telegram (if token set),
                    email stub (logs only).
"""

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from core.db import DB_PATH

# Minimum severity to send an immediate alert
# low → digest only, medium/high → immediate
IMMEDIATE_THRESHOLD = {"high", "medium"}


def route_alerts(changes: list[dict]):
    """Send alerts for classified changes. Called by pipeline.py."""
    for change in changes:
        if _is_duplicate(change):
            continue

        severity = change.get("severity", "low")
        channel = "console"

        if severity in IMMEDIATE_THRESHOLD:
            _send_console(change)
            _try_telegram(change)
            channel = "telegram" if os.getenv("TELEGRAM_BOT_TOKEN") else "console"
        else:
            _log_digest(change)
            channel = "digest"

        _record_alert(change["id"], channel)


def _is_duplicate(change: dict) -> bool:
    """Check if an identical change was already alerted in the last 2 captures."""
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT COUNT(*) FROM alerts a
           JOIN changes c ON a.change_id = c.id
           WHERE c.field = ? AND c.change_type = ?
             AND a.sent_at > datetime('now', '-30 minutes')""",
        (change["field"], change["change_type"]),
    )
    count = cursor.fetchone()[0]
    conn.close()
    return count > 0


def _send_console(change: dict):
    diff = change.get("_diff", {})
    stealth_tag = " [STEALTH]" if change.get("is_stealth") else ""
    print(
        f"\n{'='*60}\n"
        f"⚠  ALERT [{change['severity'].upper()}]{stealth_tag}\n"
        f"   Competitor : {change.get('_competitor', '?')}\n"
        f"   Market     : {change.get('_market', '?')}\n"
        f"   Field      : {change['field']}\n"
        f"   Change     : {diff.get('old_value')} → {diff.get('new_value')}\n"
        f"   Type       : {change['change_type']}\n"
        f"   Change ID  : {change['id']}\n"
        f"   Claim old  : {change.get('claim_id_old', '-')}\n"
        f"   Claim new  : {change.get('claim_id_new', '-')}\n"
        f"{'='*60}\n"
    )


def _try_telegram(change: dict):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        import httpx
        diff = change.get("_diff", {})
        stealth_tag = " 🕵 STEALTH" if change.get("is_stealth") else ""
        text = (
            f"*RivalIQ Alert [{change['severity'].upper()}]{stealth_tag}*\n"
            f"Competitor: {change.get('_competitor', '?')}\n"
            f"Market: {change.get('_market', '?')}\n"
            f"Field: `{change['field']}`\n"
            f"Change: `{diff.get('old_value')}` → `{diff.get('new_value')}`\n"
            f"Type: {change['change_type']}\n"
            f"Change ID: `{change['id']}`"
        )
        httpx.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


def _log_digest(change: dict):
    diff = change.get("_diff", {})
    print(
        f"[DIGEST] {change.get('_competitor','?')} | "
        f"{change['field']}: {diff.get('old_value')} → {diff.get('new_value')} "
        f"[{change['severity']}]"
    )


def _record_alert(change_id: str, channel: str):
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO alerts (id, change_id, channel, sent_at, feedback) "
        "VALUES (?,?,?,?,?)",
        (
            f"alt_{uuid.uuid4().hex[:12]}",
            change_id,
            channel,
            datetime.now(timezone.utc).isoformat(),
            None,
        ),
    )
    conn.commit()
    conn.close()