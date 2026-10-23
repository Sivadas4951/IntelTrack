import asyncio
import json
import logging
import sqlite3
import uuid
from datetime import datetime, timezone
import httpx
from bs4 import BeautifulSoup

from collector.compliance import compliance_guard, USER_AGENT
from core.db import DB_PATH
from core.ledger import compute_hash

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class WaybackBackfillEngine:
    """Queries Wayback CDX API to backfill historical website snapshots."""

    def __init__(self, source_id: str, target_url: str):
        self.source_id = source_id
        self.target_url = target_url
        self.cdx_api_url = "http://web.archive.org/cdx/search/cdx"

    async def fetch_cdx_index(self, limit: int = 12) -> list[dict]:
        """Fetches monthly collapsed snapshots from Wayback Machine CDX API."""
        params = {
            "url": self.target_url,
            "output": "json",
            "collapse": "timestamp:6",  # Collapse by YYYYMM (monthly)
            "limit": limit,
            "filter": "statuscode:200"
        }
        
        headers = {"User-Agent": USER_AGENT}
        
        async with httpx.AsyncClient(timeout=10.0) as client:
            try:
                logging.info(f"WAYBACK_CDX_QUERY | Source: {self.source_id} | URL: {self.target_url}")
                response = await client.get(self.cdx_api_url, params=params, headers=headers)
                if response.status_code != 200:
                    logging.warning(f"WAYBACK_CDX_FAILED | Status: {response.status_code}")
                    return []
                
                rows = response.json()
                if len(rows) <= 1:
                    return []
                
                # First row is header: ["urlkey", "timestamp", "original", "mimetype", "statuscode", "digest", "length"]
                headers_row = rows[0]
                results = []
                for row in rows[1:]:
                    entry = dict(zip(headers_row, row))
                    results.append(entry)
                
                return results
            except Exception as e:
                logging.error(f"WAYBACK_CDX_ERROR | {e}")
                return []

    async def run_backfill(self, limit: int = 6) -> int:
        """Downloads archived snapshots and inserts them with origin='archived'[cite: 2]."""
        index_entries = await self.fetch_cdx_index(limit=limit)
        if not index_entries:
            logging.info(f"WAYBACK_BACKFILL | No archived snapshots found for {self.target_url}")
            return 0

        saved_count = 0
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for entry in index_entries:
                timestamp = entry["timestamp"]  # Format: YYYYMMDDhhmmss
                wayback_url = f"http://web.archive.org/web/{timestamp}/{entry['original']}"
                
                # Format timestamp to ISO UTC string
                captured_dt = datetime.strptime(timestamp, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
                captured_at_iso = captured_dt.isoformat()

                try:
                    compliance_guard.enforce_rate_limit("http://web.archive.org")
                    resp = await client.get(wayback_url, headers={"User-Agent": USER_AGENT})
                    if resp.status_code == 200:
                        soup = BeautifulSoup(resp.text, "html.parser")
                        for tag in soup(["script", "style", "nav", "footer"]):
                            tag.decompose()
                        clean_text = soup.get_text(separator=" ", strip=True)

                        # Insert directly into ledger with origin="archived"[cite: 2]
                        snapshot_id = f"snap_arch_{uuid.uuid4().hex[:10]}"
                        c_hash = compute_hash(clean_text)
                        
                        # Use genesis chain placeholder for backfilled history
                        ledger_hash = compute_hash(f"{self.source_id}|{captured_at_iso}|{c_hash}|ARCHIVED_GENESIS")

                        conn = sqlite3.connect(DB_PATH)
                        cursor = conn.cursor()
                        cursor.execute("""
                            INSERT INTO snapshots (
                                id, source_id, captured_at, content_hash, prev_snapshot_hash,
                                ledger_hash, raw_text, screenshot_path, origin
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            snapshot_id, self.source_id, captured_at_iso, c_hash,
                            "ARCHIVED_GENESIS", ledger_hash, clean_text, None, "archived"
                        ))
                        conn.commit()
                        conn.close()
                        saved_count += 1
                        logging.info(f"WAYBACK_SNAPSHOT_SAVED | Date: {captured_at_iso} | ID: {snapshot_id}")
                except Exception as e:
                    logging.error(f"WAYBACK_DOWNLOAD_ERROR | {wayback_url} | {e}")

        return saved_count


class SimulatedBackfillLoader:
    """Loads historical datasets for Headphones and Dairy explicitly tagged as origin='simulated'[cite: 2]."""

    @staticmethod
    def seed_simulated_history():
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()

        simulated_records = [
            # Headphones historical progression
            {
                "source_id": "src_audio_1",
                "captured_at": "2026-05-15T10:00:00Z",
                "text": "SoundMax Pro TWS: Price ₹2,499. Battery: 40h. Active Noise Cancellation: Yes. Warranty: 12 Months."
            },
            {
                "source_id": "src_audio_1",
                "captured_at": "2026-08-01T10:00:00Z",
                "text": "SoundMax Pro TWS: Price ₹1,999. Battery: 40h. Active Noise Cancellation: Yes. Warranty: 12 Months."
            },
            # Dairy historical progression[cite: 1, 2]
            {
                "source_id": "src_dairy_1",
                "captured_at": "2026-04-10T08:00:00Z",
                "text": "Fresh Dairy Whole Milk: Price ₹28, Volume: 500 ml, Fat: 4.5%."
            },
            {
                "source_id": "src_dairy_1",
                "captured_at": "2026-07-20T08:00:00Z",
                "text": "Fresh Dairy Whole Milk: Price ₹30, Volume: 500 ml, Fat: 4.5%."
            }
        ]

        inserted = 0
        for rec in simulated_records:
            snap_id = f"snap_sim_{uuid.uuid4().hex[:10]}"
            c_hash = compute_hash(rec["text"])
            ledger_hash = compute_hash(f"{rec['source_id']}|{rec['captured_at']}|{c_hash}|SIMULATED_GENESIS")

            cursor.execute("""
                INSERT INTO snapshots (
                    id, source_id, captured_at, content_hash, prev_snapshot_hash,
                    ledger_hash, raw_text, screenshot_path, origin
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                snap_id, rec["source_id"], rec["captured_at"], c_hash,
                "SIMULATED_GENESIS", ledger_hash, rec["text"], None, "simulated"
            ))
            inserted += 1

        conn.commit()
        conn.close()
        logging.info(f"SIMULATED_BACKFILL_COMPLETE | Inserted {inserted} simulated historical records.")
        return inserted


if __name__ == "__main__":
    # Test Backfill Engines
    logging.info("Starting Historical Backfill Stage...")
    
    # 1. Seed simulated history[cite: 2]
    SimulatedBackfillLoader.seed_simulated_history()

    # 2. Test Wayback Machine CDX Pull[cite: 2]
    async def test_wayback():
        wb = WaybackBackfillEngine("src_isp_1", "https://example.com")
        count = await wb.run_backfill(limit=2)
        print(f"Wayback snapshots retrieved: {count}")

    asyncio.run(test_wayback())