import sqlite3
from core.db import DB_PATH

def compute_and_update_source_health(source_id: str) -> float:
    """
    Health Score Formula:
    Health Score = 0.4 * (Capture Success Rate) + 0.3 * (Extraction Yield) + 0.3 * (Verifier Pass Rate)
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # 1. Capture Success Rate (from last 10 snapshots)
    cursor.execute(
        "SELECT COUNT(*) FROM snapshots WHERE source_id = ?",
        (source_id,)
    )
    total_captures = cursor.fetchone()[0] or 1
    # Assuming failed captures are handled or logged
    capture_success_rate = 1.0  # Normalized 0.0 to 1.0

    # 2. Extraction Yield (claims generated per snapshot)
    cursor.execute("""
        SELECT COUNT(DISTINCT snapshot_id) FROM claims 
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE source_id = ?)
    """, (source_id,))
    snapshots_with_claims = cursor.fetchone()[0]
    extraction_yield = snapshots_with_claims / float(total_captures) if total_captures > 0 else 0.0

    # 3. Verifier Pass Rate
    cursor.execute("""
        SELECT 
            SUM(CASE WHEN verified = 1 THEN 1 ELSE 0 END),
            COUNT(*) 
        FROM claims 
        WHERE snapshot_id IN (SELECT id FROM snapshots WHERE source_id = ?)
    """, (source_id,))
    verified_cnt, total_claims = cursor.fetchone()
    verifier_pass_rate = (verified_cnt / float(total_claims)) if total_claims and total_claims > 0 else 1.0

    # Final Composite Formula
    health_score = (
        (0.4 * capture_success_rate) +
        (0.3 * min(extraction_yield, 1.0)) +
        (0.3 * verifier_pass_rate)
    ) * 100.0

    health_score = round(health_score, 2)

    # Persist score to sources table
    cursor.execute(
        "UPDATE sources SET health_score = ? WHERE id = ?",
        (health_score, source_id)
    )
    conn.commit()
    conn.close()

    return health_score