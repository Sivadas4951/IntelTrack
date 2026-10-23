"""
seed_demo_claims.py
-------------------
Seeds simulated sources, snapshots, claims and stealth-change rows so Person 3's
analytics can be built and demoed before the live pipeline produces data.

ALL VALUES ARE INVENTED, origin='simulated'. Label them so in the UI.
Each simulated source has its own valid hash chain.

Run (from project root):
    python -m analytics.seed_demo_claims            # seed (safe to repeat)
    python -m analytics.seed_demo_claims --reset    # delete seed rows, then seed again
    python -m analytics.seed_demo_claims --clear    # delete seed rows only
"""

import argparse
import sqlite3
from datetime import datetime, timedelta, timezone

from core.db import DB_PATH, init_db
from core.ledger import compute_hash

SIM_SRC_PREFIX = "src_simseed_"
SIM_SNAP_PREFIX = "snap_simseed_"
CLAIM_PREFIX = "SIM-"

MARKET_URLS = {
    "isp": "http://localhost:8001/isp/plans",
    "dairy": "http://localhost:8001/dairy/products",
    "headphones": "http://localhost:8001/audio/earbuds",
}

UNITS = {
    "price_inr_per_month": "INR", "price_inr": "INR",
    "speed_mbps": "Mbps", "post_fup_speed_mbps": "Mbps",
    "fup_cap_gb": "GB", "pack_size_ml": "ml", "fat_percentage": "%",
    "battery_hours": "hours", "warranty_months": "months",
}

T_BASE = "2026-10-22T10:00:00+00:00"    # before a flip
T_FLIP = "2026-10-23T11:00:00+00:00"    # after a flip
T_SINGLE = "2026-10-23T09:00:00+00:00"  # competitors with no flip

# flip: (raw field, new value, change_type). Same price, less value.
SEED = [
    # ── ISP ──
    {"market": "isp", "competitor": "SpeedFiber", "slug": "speedfiber", "plan": "Fiber Ultra", "t0": T_BASE,
     "facts": {"plan_name": "Fiber Ultra", "price_inr_per_month": "799", "speed_mbps": "200",
               "fup_cap_gb": "3300", "post_fup_speed_mbps": "10", "ott_bundle": "Disney+ Hotstar, SonyLIV"},
     "flip": ("fup_cap_gb", "1500", "quantity")},
    {"market": "isp", "competitor": "FiberNet", "slug": "fibernet", "plan": "Net Turbo", "t0": T_SINGLE,
     "facts": {"plan_name": "Net Turbo", "price_inr_per_month": "749", "speed_mbps": "150",
               "fup_cap_gb": "2500", "post_fup_speed_mbps": "10", "ott_bundle": "ZEE5"}},
    {"market": "isp", "competitor": "BharatLink", "slug": "bharatlink", "plan": "Link Max", "t0": T_SINGLE,
     "facts": {"plan_name": "Link Max", "price_inr_per_month": "899", "speed_mbps": "300",
               "fup_cap_gb": "3000", "post_fup_speed_mbps": "20", "ott_bundle": "Disney+ Hotstar, Prime Video"},
     "unverified_decoy": ("speed_mbps", "999")},
    # ── Dairy ──
    {"market": "dairy", "competitor": "FreshDairy", "slug": "freshdairy", "plan": "Fresh Whole Milk", "t0": T_BASE,
     "facts": {"product_name": "Fresh Whole Milk", "price_inr": "30", "pack_size_ml": "500", "fat_percentage": "4.5"},
     "flip": ("pack_size_ml", "450", "quantity")},
    {"market": "dairy", "competitor": "PureMoo", "slug": "puremoo", "plan": "Pure Toned Milk", "t0": T_SINGLE,
     "facts": {"product_name": "Pure Toned Milk", "price_inr": "27", "pack_size_ml": "500", "fat_percentage": "3.0"}},
    {"market": "dairy", "competitor": "GreenPasture", "slug": "greenpasture", "plan": "Green Whole Milk", "t0": T_SINGLE,
     "facts": {"product_name": "Green Whole Milk", "price_inr": "62", "pack_size_ml": "1000", "fat_percentage": "4.5"}},
    # ── Headphones ──
    {"market": "headphones", "competitor": "SoundMax", "slug": "soundmax", "plan": "SoundMax Pro TWS", "t0": T_BASE,
     "facts": {"model_name": "SoundMax Pro TWS", "price_inr": "1999", "battery_hours": "40",
               "anc_enabled": "true", "warranty_months": "12"},
     "flip": ("battery_hours", "30", "quantity")},
    {"market": "headphones", "competitor": "BassBeat", "slug": "bassbeat", "plan": "BassBeat Air", "t0": T_SINGLE,
     "facts": {"model_name": "BassBeat Air", "price_inr": "1499", "battery_hours": "30",
               "anc_enabled": "false", "warranty_months": "12"}},
    {"market": "headphones", "competitor": "ClearTone", "slug": "cleartone", "plan": "ClearTone ANC", "t0": T_SINGLE,
     "facts": {"model_name": "ClearTone ANC", "price_inr": "2499", "battery_hours": "35",
               "anc_enabled": "true", "warranty_months": "24"}},
]


def _raw_text(entry: dict, facts: dict) -> str:
    body = " ".join(f"{k}: {v}." for k, v in facts.items())
    return f"[SIMULATED] {entry['competitor']} {entry['plan']} | {body}"


def _insert_claim(cur, claim_id, snapshot_id, entry, field, value, observed_at, verified=1):
    cur.execute(
        """INSERT OR REPLACE INTO claims (
            claim_id, snapshot_id, market, competitor, plan_or_sku, field, value, unit,
            observed_at, source_url, region, verified, claim_confidence, extraction_method,
            ambiguity, cross_source_agreement, extraction_model_version, prompt_version,
            verifier_version, schema_version
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (claim_id, snapshot_id, entry["market"], entry["competitor"], entry["plan"],
         field, value, UNITS.get(field), observed_at, MARKET_URLS[entry["market"]],
         "Pune", verified, 0.95, "css", "none", "single",
         "seed_v1", "seed_v1", "seed_v1", "seed_v1"),
    )


def seed_demo_claims():
    init_db()
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    detected_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    for entry in SEED:
        market, slug = entry["market"], entry["slug"]
        source_id = f"{SIM_SRC_PREFIX}{market}_{slug}"

        cur.execute(
            "INSERT OR REPLACE INTO sources VALUES (?,?,?,?,?,?,?,?)",
            (source_id, market, entry["competitor"], MARKET_URLS[market],
             "simulated_seed", "Pune", 1440, 100.0),
        )

        timeline = [("T0", entry["t0"], dict(entry["facts"]))]
        flip = entry.get("flip")
        if flip:
            after = dict(entry["facts"])
            after[flip[0]] = flip[1]
            timeline.append(("T1", T_FLIP, after))

        prev_ledger = "GENESIS"
        for tag, observed_at, facts in timeline:
            snapshot_id = f"{SIM_SNAP_PREFIX}{market}_{slug}_{tag.lower()}"
            raw_text = _raw_text(entry, facts)
            content_hash = compute_hash(raw_text)
            ledger_hash = compute_hash(f"{source_id}|{observed_at}|{content_hash}|{prev_ledger}")

            cur.execute(
                "INSERT OR REPLACE INTO snapshots VALUES (?,?,?,?,?,?,?,?,?)",
                (snapshot_id, source_id, observed_at, content_hash, prev_ledger,
                 ledger_hash, raw_text, None, "simulated"),
            )
            prev_ledger = ledger_hash

            for field, value in facts.items():
                _insert_claim(cur, f"{CLAIM_PREFIX}{market}-{slug}-{field}-{tag}",
                              snapshot_id, entry, field, value, observed_at)

            # A later, UNVERIFIED claim to prove unverified values are ignored
            decoy = entry.get("unverified_decoy")
            if decoy and tag == "T0":
                later = (datetime.fromisoformat(observed_at) + timedelta(hours=1)).isoformat()
                _insert_claim(cur, f"{CLAIM_PREFIX}{market}-{slug}-{decoy[0]}-UNVERIFIED",
                              snapshot_id, entry, decoy[0], decoy[1], later, verified=0)

        if flip:
            field, _, change_type = flip
            cur.execute(
                """INSERT OR REPLACE INTO changes
                   (id, claim_id_old, claim_id_new, field, change_type, severity,
                    is_stealth, detected_at, status)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (f"SIM-chg-{market}-{slug}-{field}",
                 f"{CLAIM_PREFIX}{market}-{slug}-{field}-T0",
                 f"{CLAIM_PREFIX}{market}-{slug}-{field}-T1",
                 field, change_type, "high", 1, detected_at, "pending"),
            )

    conn.commit()
    conn.close()
    print(f"Seeded {len(SEED)} simulated competitors (3 stealth flips).")


def clear_demo_seed():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM cards  WHERE change_id LIKE 'SIM-%'")
    cur.execute("DELETE FROM alerts WHERE change_id LIKE 'SIM-%'")
    cur.execute("DELETE FROM changes WHERE id LIKE 'SIM-%'")
    cur.execute("DELETE FROM metrics WHERE inputs_claim_ids LIKE '%SIM-%'")
    cur.execute("DELETE FROM claims WHERE claim_id LIKE 'SIM-%'")
    cur.execute("DELETE FROM snapshots WHERE id LIKE ?", (f"{SIM_SNAP_PREFIX}%",))
    cur.execute("DELETE FROM sources WHERE id LIKE ?", (f"{SIM_SRC_PREFIX}%",))
    conn.commit()
    conn.close()
    print("Removed all simulated seed rows.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--reset", action="store_true", help="clear seed rows, then seed again")
    parser.add_argument("--clear", action="store_true", help="clear seed rows only")
    args = parser.parse_args()

    init_db()
    if args.clear:
        clear_demo_seed()
    else:
        if args.reset:
            clear_demo_seed()
        seed_demo_claims()