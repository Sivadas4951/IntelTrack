"""
digest.py
---------
Daily digest: every change in the last N hours, stealth first, then by severity.
Built straight from the `changes` + `claims` tables, so each line carries claim IDs.

CLI (from project root):
    python -m notifications.digest            # print the last 24h digest
    python -m notifications.digest --send     # also push it to configured channels
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH
from notifications.channels import send_text

_SEV_ORDER = {"high": 0, "medium": 1, "low": 2}


def build_digest(hours: int = 24) -> dict:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        """SELECT ch.id, ch.field, ch.change_type, ch.severity, ch.is_stealth,
                  ch.detected_at, ch.claim_id_old, ch.claim_id_new,
                  cn.market, cn.competitor, cn.plan_or_sku, cn.unit,
                  cn.value AS new_value, co.value AS old_value
           FROM changes ch
           LEFT JOIN claims cn ON cn.claim_id = ch.claim_id_new
           LEFT JOIN claims co ON co.claim_id = ch.claim_id_old
           WHERE ch.detected_at >= ?""",
        (cutoff,),
    ).fetchall()
    conn.close()

    items = [dict(r) for r in rows]
    items.sort(key=lambda i: (-int(i["is_stealth"]), _SEV_ORDER.get(i["severity"], 3), i["detected_at"]))

    by_sev, by_market = {}, {}
    for i in items:
        by_sev[i["severity"]] = by_sev.get(i["severity"], 0) + 1
        m = i["market"] or "unknown"
        by_market[m] = by_market.get(m, 0) + 1

    return {
        "window_hours": hours,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total": len(items),
        "stealth_count": sum(1 for i in items if i["is_stealth"]),
        "by_severity": by_sev,
        "by_market": by_market,
        "items": items,
    }


def render_digest(d: dict) -> tuple[str, str]:
    subject = (f"IntelTrack daily digest: {d['total']} change(s), "
               f"{d['stealth_count']} stealth, last {d['window_hours']}h")
    lines = [subject, ""]
    if not d["items"]:
        lines.append("No verified changes in this window.")
    for i in d["items"]:
        tag = " [STEALTH]" if i["is_stealth"] else ""
        lines.append(
            f"[{str(i['severity']).upper()}]{tag} {i['competitor'] or '?'} / "
            f"{i['plan_or_sku'] or '?'} / {i['field']}: {i['old_value']} -> {i['new_value']} "
            f"(evidence {i['claim_id_old'] or '-'} -> {i['claim_id_new'] or '-'})"
        )
    return subject, "\n".join(lines)


def send_digest(hours: int = 24) -> dict:
    d = build_digest(hours)
    subject, text = render_digest(d)
    d["delivered_to"] = send_text(subject, text)
    return d


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--send", action="store_true")
    a = ap.parse_args()
    result = send_digest(a.hours) if a.send else build_digest(a.hours)
    print(render_digest(result)[1])
    if a.send:
        print(f"\nDelivered to: {result['delivered_to'] or 'no external channel configured'}")