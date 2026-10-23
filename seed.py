import sqlite3
import hashlib
from datetime import datetime, timezone
from core.db import DB_PATH, init_db

def compute_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def seed_data():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Seed sources
    sources = [
        ("src_isp_1", "isp", "SpeedFiber", "http://localhost:8001/isp/plans", "pricing", "Pune", 15, 100.0),
        ("src_dairy_1", "dairy", "FreshDairy", "http://localhost:8001/dairy/products", "pricing", "Pune", 15, 100.0),
        ("src_audio_1", "headphones", "SoundMax", "http://localhost:8001/audio/earbuds", "pricing", "Pune", 15, 100.0)
    ]
    cursor.executemany("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)", sources)

    # T0 (Baseline Snapshot)
    t0_text = "SpeedFiber Ultra Plan: Price ₹799/month. Speed: 200 Mbps. FUP Cap: 3300 GB. Included OTT: Disney+, SonyLIV."
    t0_c_hash = compute_hash(t0_text)
    t0_prev = "GENESIS"
    t0_ledger = compute_hash(f"src_isp_1|2026-10-23T10:00:00Z|{t0_c_hash}|{t0_prev}")

    # T1 (Mutated Snapshot - Stealth FUP cut)
    t1_text = "SpeedFiber Ultra Plan: Price ₹799/month. Speed: 200 Mbps. FUP Cap: 1500 GB. Included OTT: Disney+, SonyLIV."
    t1_c_hash = compute_hash(t1_text)
    t1_prev = t0_ledger
    t1_ledger = compute_hash(f"src_isp_1|2026-10-23T11:00:00Z|{t1_c_hash}|{t1_prev}")

    snapshots = [
        ("snap_isp_t0", "src_isp_1", "2026-10-23T10:00:00Z", t0_c_hash, t0_prev, t0_ledger, t0_text, None, "simulated"),
        ("snap_isp_t1", "src_isp_1", "2026-10-23T11:00:00Z", t1_c_hash, t1_prev, t1_ledger, t1_text, None, "simulated")
    ]
    cursor.executemany("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)", snapshots)

    conn.commit()
    conn.close()
    print("Database seeded with T0 baseline and T1 mutated snapshots.")

if __name__ == "__main__":
    seed_data()