import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from collector.fetch_ladder import FetchLadder
from collector.quality_guard import CaptureQualityGuard
from collector.health_score import compute_and_update_source_health
from core.db import DB_PATH
from pipeline import run_pipeline

MAX_CAPTURE_ATTEMPTS = 3
RETRY_BASE_SECONDS = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

class IntelTrackScheduler:
    """Manages multi-tiered background polling loops and adaptive cadence boosting[cite: 2]."""

    def __init__(self, demo_mode: bool = True):
        self.scheduler = AsyncIOScheduler()
        self.demo_mode = demo_mode
        self.active_boosts: dict[str, float] = {}  # source_id -> boost_factor

    async def capture_job_worker(self, source_id: str, url: str):
        """Core asynchronous worker for executing capture, quality inspection, and health scoring[cite: 2]."""
        logging.info(f"SCHEDULER_EXECUTE | Source: {source_id} | Target: {url}")

        try:
            # 1. Execute 4-step Fetch Ladder[cite: 2]
            snapshot_meta = None
            for attempt in range(1, MAX_CAPTURE_ATTEMPTS + 1):
                try:
                    ladder = FetchLadder(source_id=source_id, url=url)
                    snapshot_meta = await ladder.execute_ladder()
                    break
                except PermissionError:
                    raise  # robots.txt block: never retry
                except Exception as e:
                    logging.warning(f"CAPTURE_RETRY | Source: {source_id} | Attempt {attempt}/{MAX_CAPTURE_ATTEMPTS} | {e}")
                    if attempt == MAX_CAPTURE_ATTEMPTS:
                        raise
                    await asyncio.sleep(RETRY_BASE_SECONDS * 2 ** (attempt - 1))
            
            raw_text = snapshot_meta.get("raw_text", "")
            
            # 2. Inspect capture for quality anomalies[cite: 2]
            is_valid, anomaly_reason = CaptureQualityGuard.inspect_capture(raw_text=raw_text)
            
            if not is_valid:
                logging.warning(f"QUALITY_GUARD_REJECT | Source: {source_id} | Reason: {anomaly_reason}")
                # Update health score downwards due to capture failure[cite: 2]
                compute_and_update_source_health(source_id)
                return

            # 3. Update Source Health Score after successful ingestion[cite: 2]
            current_health = compute_and_update_source_health(source_id)
            logging.info(f"CAPTURE_SUCCESS | Source: {source_id} | Snapshot: {snapshot_meta['snapshot_id']} | Health: {current_health}")

            # Hand the new snapshot to Person 2's pipeline (extract -> verify -> diff -> classify -> alert)
            meta = self._load_source_meta(source_id)
            if meta:
                summary = await asyncio.to_thread(
                    run_pipeline,
                    snapshot_meta["snapshot_id"], source_id, meta["market"],
                    meta["competitor"], raw_text, url, meta["region"],
                )
                logging.info(f"PIPELINE_DONE | Source: {source_id} | {summary}")

        except Exception as e:
            logging.error(f"SCHEDULER_WORKER_ERROR | Source: {source_id} | Exception: {e}")
            compute_and_update_source_health(source_id)

    @staticmethod
    def _load_source_meta(source_id: str) -> dict | None:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute(
            "SELECT market, competitor, region FROM sources WHERE id = ?", (source_id,)
        ).fetchone()
        conn.close()
        return {"market": row[0], "competitor": row[1], "region": row[2]} if row else None

    def load_and_schedule_sources(self):
        """Loads registered sources from SQLite DB and configures Fast/Slow lanes[cite: 2]."""
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT id, market, competitor, url, page_type, cadence_minutes FROM sources")
        sources = cursor.fetchall()
        conn.close()

        for src in sources:
            source_id, market, competitor, url, page_type, cadence_minutes = src
            job_id = f"job_{source_id}"

            if page_type == "pricing":
                # FAST LANE: Pricing & Plan Pages[cite: 2]
                if self.demo_mode:
                    # In demo mode, poll every 15 seconds for live feedback[cite: 2]
                    trigger = IntervalTrigger(seconds=15)
                    logging.info(f"SCHEDULE_FAST_LANE (DEMO) | Source: {source_id} | Interval: 15s")
                else:
                    trigger = IntervalTrigger(minutes=cadence_minutes)
                    logging.info(f"SCHEDULE_FAST_LANE | Source: {source_id} | Interval: {cadence_minutes}m")

                self.scheduler.add_job(
                    self.capture_job_worker,
                    trigger=trigger,
                    args=[source_id, url],
                    id=job_id,
                    replace_existing=True
                )

            elif page_type == "changelog":
                # SLOW LANE: Feature, Positioning & Announcement Pages (Daily)[cite: 2]
                trigger = CronTrigger(hour=2, minute=0)  # Daily at 02:00 AM UTC[cite: 2]
                logging.info(f"SCHEDULE_SLOW_LANE | Source: {source_id} | Schedule: Daily 02:00 UTC")

                self.scheduler.add_job(
                    self.capture_job_worker,
                    trigger=trigger,
                    args=[source_id, url],
                    id=job_id,
                    replace_existing=True
                )

    def boost_source_cadence(self, source_id: str, factor: float = 0.25, duration_hours: int = 6):
        """
        Stretch Goal: Temporarily speeds up polling cadence by 4x (factor=0.25) 
        when Person 2 detects a high-severity competitor change[cite: 2].
        """
        conn = sqlite3.connect(DB_PATH)
        cursor = conn.cursor()
        cursor.execute("SELECT url, cadence_minutes FROM sources WHERE id = ?", (source_id,))
        row = cursor.fetchone()
        conn.close()

        if not row:
            logging.error(f"BOOST_CADENCE_FAILED | Source {source_id} not found.")
            return

        url, base_cadence = row
        boosted_cadence_seconds = max(5, int((base_cadence * 60) * factor))
        job_id = f"job_{source_id}"

        logging.info(f"ADAPTIVE_CADENCE_BOOST | Source: {source_id} | Accelerating to every {boosted_cadence_seconds}s for {duration_hours}h")

        # Reschedule job with accelerated interval
        self.scheduler.reschedule_job(
            job_id=job_id,
            trigger=IntervalTrigger(seconds=boosted_cadence_seconds)
        )

        # Revert back to normal cadence after duration_hours
        async def reset_cadence_later():
            await asyncio.sleep(duration_hours * 3600)
            logging.info(f"ADAPTIVE_CADENCE_DECAY | Reverting source {source_id} to baseline cadence.")
            if self.demo_mode:
                self.scheduler.reschedule_job(job_id=job_id, trigger=IntervalTrigger(seconds=15))
            else:
                self.scheduler.reschedule_job(job_id=job_id, trigger=IntervalTrigger(minutes=base_cadence))

        asyncio.create_task(reset_cadence_later())

    def start(self):
        """Starts the scheduler event loop."""
        self.load_and_schedule_sources()
        self.scheduler.start()
        logging.info("IntelTrack Adaptive Scheduler started.")


# Entry point runner
if __name__ == "__main__":
    async def main():
        scheduler_engine = IntelTrackScheduler(demo_mode=True)
        scheduler_engine.start()

        # Keep event loop active
        try:
            while True:
                await asyncio.sleep(1)
        except (KeyboardInterrupt, SystemExit):
            logging.info("Scheduler shutting down...")

    asyncio.run(main())