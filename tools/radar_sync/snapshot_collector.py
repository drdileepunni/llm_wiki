#!/usr/bin/env python3
"""
Hourly snapshot collector — pulls the Radar chart and stores it as a
timestamped raw snapshot in db.snapshots. No CDS, no FAISS, no LLM.

Cron (hourly):
  0 * * * * cd /path/to/llm_wiki && app/.venv/bin/python -m tools.radar_sync.snapshot_collector \
      --cpmrn INTNHOSMEE738 --encounter 1 >> /tmp/snapshot_collector.log 2>&1

Usage:
  python -m tools.radar_sync.snapshot_collector --cpmrn INTNHOSMEE738 --encounter 1
  python -m tools.radar_sync.snapshot_collector --workspace 1A
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("snapshot_collector")

from tools.radar_sync.chart_puller import get_admitted_patients, pull_chart
from backend.services.emr.db import get_db


def collect(workspace: str = "1A", cpmrn: str | None = None, encounter: int = 1) -> dict:
    snapshot_at = datetime.now(timezone.utc)

    if cpmrn:
        logger.info("Using provided CPMRN=%s encounter=%d", cpmrn, encounter)
    else:
        patients = get_admitted_patients(workspace)
        if not patients:
            logger.error("No patients in workspace %s", workspace)
            return {"error": "no_patients"}
        info = patients[0]
        cpmrn    = info["CPMRN"]
        encounter = info.get("encounter", 1)
        logger.info("Selected CPMRN=%s encounter=%d", cpmrn, encounter)

    logger.info("Pulling chart for CPMRN=%s", cpmrn)
    chart = pull_chart(cpmrn, encounter)

    db = get_db()
    doc = {
        "CPMRN":       cpmrn,
        "encounter":   encounter,
        "snapshot_at": snapshot_at,
        "chart":       chart,
    }
    result = db.snapshots.insert_one(doc)
    logger.info("Snapshot stored: %s  id=%s", snapshot_at.isoformat(), result.inserted_id)

    vitals_count = len(chart.get("vitals") or [])
    docs_count   = len([d for d in (chart.get("documents") or []) if d.get("category") == "labs"])
    notes_count  = sum(
        len(n.get("content") or [])
        for n in ((chart.get("notes") or {}).get("finalNotes") or [])
    )
    return {
        "cpmrn":        cpmrn,
        "encounter":    encounter,
        "snapshot_at":  snapshot_at.isoformat(),
        "inserted_id":  str(result.inserted_id),
        "vitals_count": vitals_count,
        "labs_count":   docs_count,
        "notes_count":  notes_count,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hourly chart snapshot collector")
    parser.add_argument("--workspace", default="1A")
    parser.add_argument("--cpmrn",     default=None)
    parser.add_argument("--encounter", default=1, type=int)
    args = parser.parse_args()
    result = collect(workspace=args.workspace, cpmrn=args.cpmrn, encounter=args.encounter)
    print(json.dumps(result, indent=2, default=str))
