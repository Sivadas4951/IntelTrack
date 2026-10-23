import sqlite3
import os

DB_PATH = "data/rivaliq.db"

def init_db():
    os.makedirs("data", exist_ok=True)
    os.makedirs("data/screenshots", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Sources table
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        market TEXT NOT NULL,
        competitor TEXT NOT NULL,
        url TEXT NOT NULL,
        page_type TEXT NOT NULL,
        region TEXT NOT NULL,
        cadence_minutes INTEGER NOT NULL,
        health_score REAL DEFAULT 100.0
    );
    """)

    # 2. Snapshots table (Tamper-evident ledger)[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS snapshots (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        prev_snapshot_hash TEXT NOT NULL,
        ledger_hash TEXT NOT NULL,
        raw_text TEXT NOT NULL,
        screenshot_path TEXT,
        origin TEXT NOT NULL, -- live | archived | simulated[cite: 1, 2]
        FOREIGN KEY(source_id) REFERENCES sources(id)
    );
    """)

    # 3. Claims table[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS claims (
        claim_id TEXT PRIMARY KEY,
        snapshot_id TEXT NOT NULL,
        market TEXT NOT NULL,
        competitor TEXT NOT NULL,
        plan_or_sku TEXT NOT NULL,
        field TEXT NOT NULL,
        value TEXT NOT NULL,
        unit TEXT,
        observed_at TEXT NOT NULL,
        source_url TEXT NOT NULL,
        region TEXT NOT NULL,
        verified INTEGER NOT NULL,
        claim_confidence REAL NOT NULL,
        extraction_method TEXT NOT NULL,
        ambiguity TEXT NOT NULL,
        cross_source_agreement TEXT NOT NULL,
        extraction_model_version TEXT,
        prompt_version TEXT,
        verifier_version TEXT,
        schema_version TEXT,
        FOREIGN KEY(snapshot_id) REFERENCES snapshots(id)
    );
    """)

    # 4. Changes table[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS changes (
        id TEXT PRIMARY KEY,
        claim_id_old TEXT,
        claim_id_new TEXT NOT NULL,
        field TEXT NOT NULL,
        change_type TEXT NOT NULL,
        severity TEXT NOT NULL,
        is_stealth INTEGER NOT NULL,
        detected_at TEXT NOT NULL,
        status TEXT NOT NULL
    );
    """)

    # 5. Alerts table[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS alerts (
        id TEXT PRIMARY KEY,
        change_id TEXT NOT NULL,
        channel TEXT NOT NULL,
        sent_at TEXT NOT NULL,
        feedback TEXT,
        FOREIGN KEY(change_id) REFERENCES changes(id)
    );
    """)

    # 6. Metrics table[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS metrics (
        id TEXT PRIMARY KEY,
        market TEXT NOT NULL,
        competitor TEXT NOT NULL,
        metric_name TEXT NOT NULL,
        value REAL NOT NULL,
        computed_at TEXT NOT NULL,
        inputs_claim_ids TEXT NOT NULL
    );
    """)

    # 7. Counter-Move Cards table[cite: 1, 2]
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS cards (
        id TEXT PRIMARY KEY,
        change_id TEXT NOT NULL,
        body_json TEXT NOT NULL,
        owner TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL, -- pending | approved | dismissed | expired[cite: 1, 2]
        FOREIGN KEY(change_id) REFERENCES changes(id)
    );
    """)

    conn.commit()
    conn.close()

if __name__ == "__main__":
    init_db()
    print("Database schema successfully initialized.")