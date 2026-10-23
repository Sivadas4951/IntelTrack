"""
report_store.py
---------------
Storage for generated reports (our own table p3_reports; the shared schema is untouched).
Only reports that passed the checker are saved by the builder.
"""

import json
import sqlite3

from core.db import DB_PATH
from analytics.claim_store import now_iso


def ensure_tables():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS p3_reports (
            report_id TEXT PRIMARY KEY,
            report_type TEXT NOT NULL,
            market TEXT NOT NULL,
            subject TEXT NOT NULL,
            created_at TEXT NOT NULL,
            checker_passed INTEGER NOT NULL,
            sentence_count INTEGER NOT NULL,
            body_json TEXT NOT NULL
        )
    """)
    conn.commit()
    conn.close()


def save_report(report: dict, checker_passed: bool) -> str:
    """Insert or replace (report IDs are deterministic, so re-running overwrites)."""
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR REPLACE INTO p3_reports "
        "(report_id, report_type, market, subject, created_at, checker_passed, sentence_count, body_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (report["report_id"], report["report_type"], report["market"], report["subject"],
         report.get("created_at") or now_iso(), 1 if checker_passed else 0,
         len(report.get("sentences", [])), json.dumps(report)),
    )
    conn.commit()
    conn.close()
    return report["report_id"]


def _row(row) -> dict:
    d = dict(row)
    d["report"] = json.loads(d.pop("body_json"))
    return d


def get_report(report_id: str) -> dict | None:
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT * FROM p3_reports WHERE report_id = ?", (report_id,)).fetchone()
    conn.close()
    return _row(row) if row else None


def list_reports(market: str | None = None, report_type: str | None = None) -> list[dict]:
    ensure_tables()
    sql, params = "SELECT * FROM p3_reports WHERE 1=1", []
    if market:
        sql += " AND market = ?"
        params.append(market)
    if report_type:
        sql += " AND report_type = ?"
        params.append(report_type)
    sql += " ORDER BY report_type, market, report_id"
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    return [_row(r) for r in rows]


def set_checker_result(report_id: str, passed: bool):
    ensure_tables()
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE p3_reports SET checker_passed = ? WHERE report_id = ?",
                 (1 if passed else 0, report_id))
    conn.commit()
    conn.close()