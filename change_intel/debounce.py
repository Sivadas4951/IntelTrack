"""
debounce.py
-----------
Stage 4b: a change must appear in 2 consecutive captures before it can alert.

How it works (per source):
  capture N   : diff(N-1, N) shows a change   -> stored as 'pending', NOT passed on
  capture N+1 : the same (plan, field) still holds the same new value
                                              -> 'confirmed', passed on to the classifier
                value is different again      -> 'dropped' (flap / transient noise)

Set DEBOUNCE_REQUIRED=1 to bypass (useful for a fast live demo or unit runs).
Default is 2, as the IntelTrack workflow specifies.

Usage (pipeline.py):
    diffs = filter_diffs(source_id, snapshot_id, get_diffs_for_source(source_id))
"""

import os
import json
import uuid
import sqlite3
from datetime import datetime, timezone

from core.db import DB_PATH

REQUIRED_CONFIRMATIONS = int(os.getenv("DEBOUNCE_REQUIRED", "2"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_table():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS pending_changes (
            id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            snapshot_id TEXT NOT NULL,
            plan_or_sku TEXT NOT NULL,
            field TEXT NOT NULL,
            new_value TEXT,
            diff_json TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            status TEXT NOT NULL      -- pending | confirmed | dropped
        )"""
    )
    conn.commit()
    conn.close()


def _current_value(cur, snapshot_id: str, plan: str, field: str):
    """Verified value of (plan, field) in this snapshot, or None if absent."""
    row = cur.execute(
        """SELECT value FROM claims
           WHERE snapshot_id = ? AND plan_or_sku = ? AND field = ? AND verified = 1""",
        (snapshot_id, plan, field),
    ).fetchone()
    return row[0] if row else None


def filter_diffs(source_id: str, snapshot_id: str, diffs: list[dict]) -> list[dict]:
    """
    Returns only the diffs that are confirmed by this snapshot.
    Newly seen diffs are parked as pending and released on the next capture.
    """
    if REQUIRED_CONFIRMATIONS <= 1:
        return diffs

    ensure_table()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    confirmed: list[dict] = []

    # 1) Resolve earlier pending candidates against THIS snapshot.
    pending = cur.execute(
        """SELECT id, snapshot_id, plan_or_sku, field, new_value, diff_json
           FROM pending_changes WHERE source_id = ? AND status = 'pending'""",
        (source_id,),
    ).fetchall()

    for pid, pend_snap, plan, field, new_value, diff_json in pending:
        if pend_snap == snapshot_id:
            continue  # same capture re-processed: still waiting for the next one
        now_value = _current_value(cur, snapshot_id, plan, field)
        if str(now_value) == str(new_value):
            confirmed.append(json.loads(diff_json))
            status = "confirmed"
        else:
            status = "dropped"
        cur.execute("UPDATE pending_changes SET status = ? WHERE id = ?", (status, pid))

    # 2) Park this snapshot's fresh diffs as pending.
    already_confirmed = {(d["plan_or_sku"], d["field"]) for d in confirmed}
    already_pending = {(p[2], p[3]) for p in pending if p[1] == snapshot_id}
    for d in diffs:
        key = (d["plan_or_sku"], d["field"])
        if key in already_confirmed or key in already_pending:
            continue
        cur.execute(
            """INSERT INTO pending_changes
               (id, source_id, snapshot_id, plan_or_sku, field, new_value,
                diff_json, first_seen_at, status)
               VALUES (?,?,?,?,?,?,?,?, 'pending')""",
            (str(uuid.uuid4()), source_id, snapshot_id, d["plan_or_sku"], d["field"],
             None if d.get("new_value") is None else str(d["new_value"]),
             json.dumps(d, default=str), _now()),
        )

    conn.commit()
    conn.close()
    print(f"[DEBOUNCE] source={source_id} confirmed={len(confirmed)} "
          f"held={len(diffs) - len([1 for d in diffs if (d['plan_or_sku'], d['field']) in already_confirmed])}")
    return confirmed