#!/usr/bin/env python3
"""
One-off migration: apply _filter_vitals() to all existing snapshots in db.snapshots.

Drops unverified vitals that have no abnormal_list entries — the same rule now
applied at ingestion time in chart_puller._filter_vitals().

Usage:
    # Dry run (no writes) — shows what would be changed
    python -m tools.radar_sync.migrate_filter_vitals

    # Live run
    python -m tools.radar_sync.migrate_filter_vitals --apply
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
sys.path.insert(0, str(_ROOT))

from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("migrate_filter_vitals")

from backend.services.emr.db import get_db


def _keep(v: dict) -> bool:
    """Mirror of chart_puller._filter_vitals rule."""
    return v.get("isVerified") is True or bool(v.get("abnormal_list"))


def run(apply: bool) -> None:
    db = get_db()
    snaps = list(db.snapshots.find({}, {"_id": 1, "CPMRN": 1, "snapshot_at": 1, "chart.vitals": 1}))

    logger.info("Mode: %s", "LIVE" if apply else "DRY RUN")
    logger.info("Snapshots to process: %d", len(snaps))

    total_before = 0
    total_after  = 0
    modified     = 0

    for snap in snaps:
        vitals = (snap.get("chart") or {}).get("vitals") or []
        kept   = [v for v in vitals if _keep(v)]

        total_before += len(vitals)
        total_after  += len(kept)

        if len(kept) == len(vitals):
            continue  # nothing to drop for this snapshot

        modified += 1
        dropped = len(vitals) - len(kept)
        logger.info(
            "  %s  %s  vitals %d → %d  (-%d)",
            snap.get("CPMRN", "?"),
            str(snap.get("snapshot_at", ""))[:16],
            len(vitals), len(kept), dropped,
        )

        if apply:
            db.snapshots.update_one(
                {"_id": snap["_id"]},
                {"$set": {"chart.vitals": kept}},
            )

    logger.info("")
    logger.info("─" * 50)
    logger.info("Snapshots examined : %d", len(snaps))
    logger.info("Snapshots modified : %d", modified)
    logger.info("Vitals before      : %d", total_before)
    logger.info("Vitals after       : %d", total_after)
    logger.info("Vitals dropped     : %d  (%.1f%%)", total_before - total_after,
                (total_before - total_after) / total_before * 100 if total_before else 0)

    if not apply:
        logger.info("")
        logger.info("Dry run complete — re-run with --apply to write changes.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Backfill vital filter to existing snapshots")
    parser.add_argument("--apply", action="store_true", help="Write changes to MongoDB (default: dry run)")
    args = parser.parse_args()
    run(apply=args.apply)
