"""
pipeline.py
-----------
Glue function called by Person 1's scheduler after every new snapshot.

Usage (P1 calls this after recording a snapshot):
    from pipeline import run_pipeline
    await run_pipeline(snapshot_id, source_id, market, competitor,
                       raw_text, source_url, region)

Or run standalone for a seeded snapshot:
    python pipeline.py
"""

import sqlite3
from core.db import DB_PATH
from extraction.extractor import extract_claims
from verification.verifier import verify_claims
from change_intel.differ import get_diffs_for_source
from change_intel.classifier import classify_diffs
from change_intel.alert_router import route_alerts


def run_pipeline(
    snapshot_id: str,
    source_id: str,
    market: str,
    competitor: str,
    raw_text: str,
    source_url: str,
    region: str,
) -> dict:
    """
    Full P2 pipeline for one snapshot.
    Returns a summary dict with counts.
    """
    print(f"\n[PIPELINE] snapshot={snapshot_id} source={source_id} market={market}")

    # 1. Extract
    claims = extract_claims(
        snapshot_id=snapshot_id,
        source_id=source_id,
        market=market,
        competitor=competitor,
        raw_text=raw_text,
        source_url=source_url,
        region=region,
    )
    print(f"[PIPELINE] Extracted {len(claims)} claims")

    # 2. Verify
    claims = verify_claims(claims, raw_text)
    verified_count = sum(1 for c in claims if c["verified"] == 1)
    print(f"[PIPELINE] Verified {verified_count}/{len(claims)} claims")

    # 3. Write claims to DB
    _write_claims(claims)

    # 4. Diff
    diffs = get_diffs_for_source(source_id)
    print(f"[PIPELINE] Found {len(diffs)} raw diffs")

    # 5. Classify + detect stealth
    changes = classify_diffs(diffs, market=market, competitor=competitor)
    stealth_count = sum(1 for c in changes if c["is_stealth"])
    print(f"[PIPELINE] Changes: {len(changes)} (stealth: {stealth_count})")

    # 6. Route alerts
    route_alerts(changes)

    return {
        "snapshot_id": snapshot_id,
        "claims_extracted": len(claims),
        "claims_verified": verified_count,
        "diffs": len(diffs),
        "changes": len(changes),
        "stealth_changes": stealth_count,
    }


def _write_claims(claims: list[dict]):
    """Insert verified claims into the claims table."""
    if not claims:
        return
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    for c in claims:
        cursor.execute(
            """INSERT OR IGNORE INTO claims (
                claim_id, snapshot_id, market, competitor, plan_or_sku,
                field, value, unit, observed_at, source_url, region,
                verified, claim_confidence, extraction_method, ambiguity,
                cross_source_agreement, extraction_model_version,
                prompt_version, verifier_version, schema_version
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                c["claim_id"], c["snapshot_id"], c["market"], c["competitor"],
                c["plan_or_sku"], c["field"], c["value"], c["unit"],
                c["observed_at"], c["source_url"], c["region"],
                c["verified"], c["claim_confidence"], c["extraction_method"],
                c["ambiguity"], c["cross_source_agreement"],
                c["extraction_model_version"], c.get("prompt_version"),
                c.get("verifier_version"), c["schema_version"],
            ),
        )
    conn.commit()
    conn.close()


# ── Standalone runner: processes all seeded snapshots ─────────────────────

if __name__ == "__main__":
    from core.db import init_db
    init_db()

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        """SELECT s.id, s.source_id, s.raw_text, s.origin,
                  src.market, src.competitor, src.url, src.region
           FROM snapshots s
           JOIN sources src ON s.source_id = src.id
           ORDER BY s.captured_at ASC"""
    )
    rows = cursor.fetchall()
    conn.close()

    for row in rows:
        snap_id, src_id, raw_text, origin, market, competitor, url, region = row
        run_pipeline(
            snapshot_id=snap_id,
            source_id=src_id,
            market=market,
            competitor=competitor,
            raw_text=raw_text,
            source_url=url,
            region=region,
        )

    print("\n[PIPELINE] Done — all seeded snapshots processed.")