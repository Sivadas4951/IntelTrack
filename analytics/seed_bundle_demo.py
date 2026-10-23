"""
seed_bundle_demo.py
-------------------
Seeds two SIMULATED ISP bundle-drop changes for Stage 6a (all values invented, origin='simulated').

  AirWave   Air Plus    drops SonyLIV          -> no cited public price  -> "unquantified value loss"
                        (plus an UNVERIFIED decoy price claim that must be ignored)
  NetStream Stream Pro  drops Disney+ Hotstar  -> a cited public price exists (simulated price list)

The bundle flips happen at their own timestamp (after the SpeedFiber/FreshDairy/SoundMax flips)
so that the ranking-impact lines of a bundle report are not polluted by a different change.

The OTT price list is a simulated source. The price claim is stored on the NetStream plan
row so it does not appear as a separate competitor in ranking tables.

Run from the project root:
    python -m analytics.seed_bundle_demo            # seed (safe to repeat)
    python -m analytics.seed_bundle_demo --remove   # remove only these bundle demo rows
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH, init_db
from core.ledger import compute_hash
from analytics.seed_demo_claims import (
    CLAIM_PREFIX,
    MARKET_URLS,
    SIM_SNAP_PREFIX,
    SIM_SRC_PREFIX,
    T_BASE,
    _insert_claim,
    _raw_text,
)
from analytics.bundle_decomposition import price_field

OTT_PRICE_URL = "http://localhost:8001/ott/pricing"   # simulated; not served by the mock server
PRICE_SRC_ID = f"{SIM_SRC_PREFIX}isp_ott_price_list"
PRICE_SNAP_ID = f"{SIM_SNAP_PREFIX}isp_ott_price_list_t0"
BUNDLE_T_FLIP = "2026-10-23T12:00:00+00:00"   # later than the other demo flips (11:00)

PLANS = [
    {"slug": "airwave", "competitor": "AirWave", "plan": "Air Plus",
     "facts": {"plan_name": "Air Plus", "price_inr_per_month": "649", "speed_mbps": "100",
               "fup_cap_gb": "1000", "post_fup_speed_mbps": "5",
               "ott_bundle": "Disney+ Hotstar, SonyLIV"},
     "new_bundle": "Disney+ Hotstar",
     "decoy_price": ("SonyLIV", "499")},
    {"slug": "netstream", "competitor": "NetStream", "plan": "Stream Pro",
     "facts": {"plan_name": "Stream Pro", "price_inr_per_month": "699", "speed_mbps": "120",
               "fup_cap_gb": "1200", "post_fup_speed_mbps": "10",
               "ott_bundle": "Disney+ Hotstar, Prime Video"},
     "new_bundle": "Prime Video",
     "cited_price": ("Disney+ Hotstar", "299")},
]

_CLAIM_SQL = """INSERT OR REPLACE INTO claims (
    claim_id, snapshot_id, market, competitor, plan_or_sku, field, value, unit,
    observed_at, source_url, region, verified, claim_confidence, extraction_method,
    ambiguity, cross_source_agreement, extraction_model_version, prompt_version,
    verifier_version, schema_version
) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _insert_price_claim(cur, claim_id, plan, field, value, observed_at, verified):
    cur.execute(_CLAIM_SQL, (
        claim_id, PRICE_SNAP_ID, "isp", plan["competitor"], plan["plan"], field, value, "INR",
        observed_at, OTT_PRICE_URL, "Pune", verified, 0.95, "css", "none", "single",
        "seed_v1", "seed_v1", "seed_v1", "seed_v1",
    ))


def seed_bundle_demo():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    detected_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    # Simulated OTT price list source + one snapshot (its own valid hash chain)
    cur.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)",
                (PRICE_SRC_ID, "isp", "OTT public price list", OTT_PRICE_URL,
                 "simulated_seed", "Pune", 1440, 100.0))
    price_text = "[SIMULATED] OTT public price list | Disney+ Hotstar: 299 INR per month."
    content_hash = compute_hash(price_text)
    ledger_hash = compute_hash(f"{PRICE_SRC_ID}|{T_BASE}|{content_hash}|GENESIS")
    cur.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                (PRICE_SNAP_ID, PRICE_SRC_ID, T_BASE, content_hash, "GENESIS",
                 ledger_hash, price_text, None, "simulated"))

    for plan in PLANS:
        slug = plan["slug"]
        entry = {"market": "isp", "competitor": plan["competitor"], "plan": plan["plan"]}
        source_id = f"{SIM_SRC_PREFIX}isp_{slug}"
        cur.execute("INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)",
                    (source_id, "isp", plan["competitor"], MARKET_URLS["isp"],
                     "simulated_seed", "Pune", 1440, 100.0))

        after = dict(plan["facts"])
        after["ott_bundle"] = plan["new_bundle"]
        prev_ledger = "GENESIS"
        for tag, observed_at, facts in (("T0", T_BASE, plan["facts"]), ("T1", BUNDLE_T_FLIP, after)):
            snapshot_id = f"{SIM_SNAP_PREFIX}isp_{slug}_{tag.lower()}"
            raw_text = _raw_text(entry, facts)
            content_hash = compute_hash(raw_text)
            ledger_hash = compute_hash(f"{source_id}|{observed_at}|{content_hash}|{prev_ledger}")
            cur.execute("INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                        (snapshot_id, source_id, observed_at, content_hash, prev_ledger,
                         ledger_hash, raw_text, None, "simulated"))
            prev_ledger = ledger_hash
            for field, value in facts.items():
                _insert_claim(cur, f"{CLAIM_PREFIX}isp-{slug}-{field}-{tag}",
                              snapshot_id, entry, field, value, observed_at)

        cur.execute(
            """INSERT OR REPLACE INTO changes
               (id, claim_id_old, claim_id_new, field, change_type, severity,
                is_stealth, detected_at, status)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (f"SIM-chg-isp-{slug}-ott_bundle",
             f"{CLAIM_PREFIX}isp-{slug}-ott_bundle-T0", f"{CLAIM_PREFIX}isp-{slug}-ott_bundle-T1",
             "ott_bundle", "bundle", "high", 1, detected_at, "pending"))

        if plan.get("cited_price"):
            item, value = plan["cited_price"]
            field = price_field(item)
            _insert_price_claim(cur, f"{CLAIM_PREFIX}isp-{slug}-{field}-T0",
                                plan, field, value, T_BASE, 1)
        if plan.get("decoy_price"):
            item, value = plan["decoy_price"]
            field = price_field(item)
            later = (datetime.fromisoformat(T_BASE) + timedelta(hours=1)).isoformat()
            _insert_price_claim(cur, f"{CLAIM_PREFIX}isp-{slug}-{field}-UNVERIFIED",
                                plan, field, value, later, 0)

    conn.commit()
    conn.close()
    print("Seeded 2 simulated ISP bundle changes: AirWave (no cited price, plus 1 unverified "
          "decoy price) and NetStream (1 cited OTT price).")


def remove_bundle_demo():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    change_ids = [f"SIM-chg-isp-{p['slug']}-ott_bundle" for p in PLANS]
    for cid in change_ids:
        for table, col in (("cards", "change_id"), ("alerts", "change_id"), ("changes", "id"),
                           ("p3_change_analysis", "change_id")):
            try:
                cur.execute(f"DELETE FROM {table} WHERE {col} = ?", (cid,))
            except sqlite3.OperationalError:
                pass  # table not created yet
    for p in PLANS:
        slug = p["slug"]
        cur.execute("DELETE FROM metrics WHERE inputs_claim_ids LIKE ?", (f"%SIM-isp-{slug}-%",))
        cur.execute("DELETE FROM claims WHERE claim_id LIKE ?", (f"SIM-isp-{slug}-%",))
        cur.execute("DELETE FROM snapshots WHERE id LIKE ?", (f"{SIM_SNAP_PREFIX}isp_{slug}_%",))
        cur.execute("DELETE FROM sources WHERE id = ?", (f"{SIM_SRC_PREFIX}isp_{slug}",))
    cur.execute("DELETE FROM snapshots WHERE id = ?", (PRICE_SNAP_ID,))
    cur.execute("DELETE FROM sources WHERE id = ?", (PRICE_SRC_ID,))
    conn.commit()
    conn.close()
    print("Removed the bundle demo rows (stored reports for them, if any, are overwritten when rebuilt).")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Stage 6a bundle demo seed")
    parser.add_argument("--remove", action="store_true", help="remove only the bundle demo rows")
    args = parser.parse_args()
    if args.remove:
        remove_bundle_demo()
    else:
        seed_bundle_demo()