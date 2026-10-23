"""
historical_loaders.py
---------------------
Stage 1 gap-fill: Common Crawl + Kaggle history loaders.
(Wayback and simulated loaders already live in collector/backfill.py.)

Every row is written to `snapshots` with an honest origin label:
    'archived'  : real third-party record (Common Crawl page, Kaggle dataset row)
    'simulated' : generated data

Usage (from the project root; nothing runs on import):
    python -m collector.historical_loaders commoncrawl --source-id src_isp_1 \
        --url https://www.example-isp.in/plans --limit 6
    python -m collector.historical_loaders kaggle --source-id src_audio_1 \
        --csv data/kaggle/headphones.csv --date-col date --name-col model \
        --price-col price --battery-col battery_hours --anc-col anc --warranty-col warranty

Notes:
  * Common Crawl only holds pages that were crawled; the mock localhost sites
    will return nothing. Point --url at a real, public page you are allowed to use.
  * Kaggle: download the CSV yourself (check the dataset licence) and pass its path.
    No Kaggle API key is needed.
  * Text is written in the same 'Label: value' format extractor.py parses, so
    archived rows flow through extract -> verify -> diff like live ones.
"""

import argparse
import csv
import gzip
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

CC_COLLINFO = "https://index.commoncrawl.org/collinfo.json"
CC_DATA = "https://data.commoncrawl.org/"


# ── shared insert ────────────────────────────────────────────────────────────

def _insert_snapshot(cur, source_id: str, captured_at: str, text: str, origin: str) -> bool:
    """Insert one historical snapshot. Skips exact duplicates. Returns True if inserted."""
    c_hash = compute_hash(text)
    dup = cur.execute(
        "SELECT 1 FROM snapshots WHERE source_id = ? AND captured_at = ? AND content_hash = ?",
        (source_id, captured_at, c_hash),
    ).fetchone()
    if dup:
        return False
    genesis = f"{origin.upper()}_GENESIS"
    ledger_hash = compute_hash(f"{source_id}|{captured_at}|{c_hash}|{genesis}")
    cur.execute(
        """INSERT INTO snapshots (id, source_id, captured_at, content_hash,
               prev_snapshot_hash, ledger_hash, raw_text, screenshot_path, origin)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (f"snap_{origin[:3]}_{uuid.uuid4().hex[:10]}", source_id, captured_at, c_hash,
         genesis, ledger_hash, text, None, origin),
    )
    return True


def _competitor(cur, source_id: str) -> str:
    row = cur.execute("SELECT competitor FROM sources WHERE id = ?", (source_id,)).fetchone()
    if not row:
        raise ValueError(f"Unknown source_id '{source_id}'. Run seed.py first.")
    return row[0]


# ── Common Crawl ─────────────────────────────────────────────────────────────

class CommonCrawlLoader:
    """Looks a URL up in recent Common Crawl indexes and stores the page text as 'archived'."""

    def __init__(self, source_id: str, url: str):
        self.source_id = source_id
        self.url = url

    def _collections(self, client: httpx.Client, n: int) -> list[str]:
        compliance_guard.enforce_rate_limit(CC_COLLINFO)
        r = client.get(CC_COLLINFO, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        return [c["cdx-api"] for c in r.json()[:n]]  # newest first

    @staticmethod
    def _warc_text(client: httpx.Client, rec: dict) -> str | None:
        """Download one WARC record by byte range and return visible page text."""
        start = int(rec["offset"])
        end = start + int(rec["length"]) - 1
        compliance_guard.enforce_rate_limit(CC_DATA)
        r = client.get(
            CC_DATA + rec["filename"],
            headers={"User-Agent": USER_AGENT, "Range": f"bytes={start}-{end}"},
            timeout=60,
        )
        if r.status_code not in (200, 206):
            return None
        raw = gzip.decompress(r.content)
        # WARC header \r\n\r\n HTTP header \r\n\r\n body
        parts = raw.split(b"\r\n\r\n", 2)
        if len(parts) < 3:
            return None
        html = parts[2].decode("utf-8", errors="ignore")
        text = BeautifulSoup(html, "html.parser").get_text("\n", strip=True)
        return text if len(text) > 100 else None

    def run(self, limit: int = 6) -> int:
        inserted = 0
        with httpx.Client(follow_redirects=True) as client:
            indexes = self._collections(client, limit)
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            for api in indexes:
                try:
                    compliance_guard.enforce_rate_limit(api)
                    r = client.get(
                        api,
                        params={"url": self.url, "output": "json", "limit": 1, "filter": "status:200"},
                        headers={"User-Agent": USER_AGENT},
                        timeout=60,
                    )
                    if r.status_code != 200 or not r.text.strip():
                        continue
                    import json
                    rec = json.loads(r.text.splitlines()[0])
                    text = self._warc_text(client, rec)
                    if not text:
                        continue
                    ts = datetime.strptime(rec["timestamp"], "%Y%m%d%H%M%S").replace(
                        tzinfo=timezone.utc).isoformat()
                    if _insert_snapshot(cur, self.source_id, ts, text, "archived"):
                        inserted += 1
                except Exception as e:
                    logging.warning(f"COMMONCRAWL_SKIP | {api} | {e}")
            conn.commit()
            conn.close()
        logging.info(f"COMMONCRAWL_COMPLETE | {self.source_id} | inserted {inserted} archived snapshots")
        return inserted


# ── Kaggle (local CSV) ───────────────────────────────────────────────────────

class KaggleCsvLoader:
    """
    Turns rows of a downloaded Kaggle CSV into historical snapshots.
    Market is read from the source row; text is built in extractor-friendly format.
    Missing columns are simply omitted from the text ("not found", never guessed).
    """

    def __init__(self, source_id: str, csv_path: str, cols: dict, origin: str = "archived"):
        self.source_id = source_id
        self.csv_path = csv_path
        self.cols = cols            # logical field -> csv column name (or None)
        self.origin = origin

    @staticmethod
    def _num(v) -> str | None:
        if v is None:
            return None
        s = str(v).replace(",", "").replace("₹", "").strip()
        try:
            f = float(s)
            return str(int(f)) if f == int(f) else str(f)
        except ValueError:
            return None

    def _get(self, row: dict, logical: str):
        col = self.cols.get(logical)
        return row.get(col) if col else None

    def _text(self, market: str, competitor: str, row: dict) -> str | None:
        name = (self._get(row, "name") or "").strip()
        price = self._num(self._get(row, "price"))
        if not price:
            return None  # a row with no price is not a usable observation
        if market == "headphones":
            parts = [f"{competitor} {name}".strip() + f": ₹{int(float(price)):,}."]
            b = self._num(self._get(row, "battery"))
            if b:
                parts.append(f"Playtime: {b} Hours.")
            anc = (self._get(row, "anc") or "").strip().lower()
            if anc:
                parts.append(f"Active Noise Cancellation: {'Yes' if anc in ('1','yes','true','y') else 'No'}.")
            w = self._num(self._get(row, "warranty"))
            if w:
                parts.append(f"Warranty: {w} Months.")
            return " ".join(parts)
        if market == "dairy":
            parts = [f"{competitor} {name}".strip() + f": ₹{price}."]
            ml = self._num(self._get(row, "pack_ml"))
            if ml:
                parts.append(f"Pack Volume: {ml} ml.")
            fat = self._num(self._get(row, "fat"))
            if fat:
                parts.append(f"Fat Content: {fat}%.")
            return " ".join(parts)
        if market == "isp":
            parts = [f"{competitor} {name} Plan: Price ₹{price}/month."]
            sp = self._num(self._get(row, "speed"))
            if sp:
                parts.append(f"Speed: {sp} Mbps.")
            fup = self._num(self._get(row, "fup"))
            if fup:
                parts.append(f"FUP Cap: {fup} GB.")
            return " ".join(parts)
        return None

    @staticmethod
    def _iso(date_str: str) -> str | None:
        for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
            try:
                return datetime.strptime(date_str.strip(), fmt).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
        return None

    def run(self) -> int:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        competitor = _competitor(cur, self.source_id)
        market = cur.execute("SELECT market FROM sources WHERE id = ?", (self.source_id,)).fetchone()[0]
        inserted = skipped = 0
        with open(self.csv_path, newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                ts = self._iso(self._get(row, "date") or "")
                text = self._text(market, competitor, row)
                if not ts or not text:
                    skipped += 1
                    continue
                if _insert_snapshot(cur, self.source_id, ts, text, self.origin):
                    inserted += 1
        conn.commit()
        conn.close()
        logging.info(f"KAGGLE_COMPLETE | {self.source_id} | inserted {inserted}, skipped {skipped}")
        return inserted


# ── CLI ──────────────────────────────────────────────────────────────────────

def _main():
    ap = argparse.ArgumentParser(description="Historical loaders: Common Crawl / Kaggle CSV")
    sub = ap.add_subparsers(dest="cmd", required=True)

    cc = sub.add_parser("commoncrawl")
    cc.add_argument("--source-id", required=True)
    cc.add_argument("--url", required=True)
    cc.add_argument("--limit", type=int, default=6)

    kg = sub.add_parser("kaggle")
    kg.add_argument("--source-id", required=True)
    kg.add_argument("--csv", required=True)
    kg.add_argument("--origin", default="archived", choices=["archived", "simulated"])
    for logical in ("date", "name", "price", "battery", "anc", "warranty",
                    "pack-ml", "fat", "speed", "fup"):
        kg.add_argument(f"--{logical}-col")

    a = ap.parse_args()
    if a.cmd == "commoncrawl":
        CommonCrawlLoader(a.source_id, a.url).run(a.limit)
    else:
        cols = {k: getattr(a, f"{k}_col".replace("-", "_"), None)
                for k in ("date", "name", "price", "battery", "anc", "warranty", "fat", "speed", "fup")}
        cols["pack_ml"] = getattr(a, "pack_ml_col", None)
        KaggleCsvLoader(a.source_id, a.csv, cols, a.origin).run()


if __name__ == "__main__":
    _main()