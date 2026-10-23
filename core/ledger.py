import hashlib
import sqlite3
import os
from datetime import datetime, timezone
from core.db import DB_PATH

def compute_hash(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()

def get_last_ledger_hash(source_id: str) -> str:
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT ledger_hash FROM snapshots WHERE source_id = ? ORDER KEY captured_at DESC LIMIT 1",
        (source_id,)
    )
    row = cursor.fetchone()
    conn.close()
    return row[0] if row else "GENESIS"

def record_snapshot(
    snapshot_id: str,
    source_id: str,
    raw_text: str,
    screenshot_bytes: bytes | None,
    screenshot_path: str | None,
    origin: str = "live"
) -> dict:
    captured_at = datetime.now(timezone.utc).isoformat()
    
    # Content hash = sha256(raw_text + screenshot_bytes)
    text_bytes = raw_text.encode("utf-8")
    combined_bytes = text_bytes + (screenshot_bytes if screenshot_bytes else b"")
    content_hash = compute_hash(combined_bytes)

    # Hash chain calculation
    prev_snapshot_hash = get_last_ledger_hash(source_id)
    ledger_input = f"{source_id}|{captured_at}|{content_hash}|{prev_snapshot_hash}"
    ledger_hash = compute_hash(ledger_input)

    # Persist screenshot if provided
    if screenshot_bytes and screenshot_path:
        os.makedirs(os.path.dirname(screenshot_path), exist_ok=True)
        with open(screenshot_path, "wb") as f:
            f.write(screenshot_bytes)

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO snapshots (
            id, source_id, captured_at, content_hash, prev_snapshot_hash,
            ledger_hash, raw_text, screenshot_path, origin
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        snapshot_id, source_id, captured_at, content_hash,
        prev_snapshot_hash, ledger_hash, raw_text, screenshot_path, origin
    ))
    conn.commit()
    conn.close()

    return {
        "snapshot_id": snapshot_id,
        "content_hash": content_hash,
        "prev_snapshot_hash": prev_snapshot_hash,
        "ledger_hash": ledger_hash,
        "captured_at": captured_at
    }