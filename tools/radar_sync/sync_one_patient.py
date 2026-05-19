#!/usr/bin/env python3
"""
Hourly live patient CDS pipeline — single patient from workspace 1A.

Steps:
  1. Discover the first patient in workspace 1A
  2. Pull full chart from Radar EMR
  3. Upsert chart to local MongoDB (patients collection)
  4. Rebuild FAISS note index → save to MongoDB (note_indexes collection)
  5. Extract delta vs last snapshot
  6. Update rolling summary (LLM call)
  7. Run CDS → get structured suggestions
  8. Append to patient_contexts.hourly_entries

Usage:
  python -m tools.radar_sync.sync_one_patient
  python -m tools.radar_sync.sync_one_patient --workspace 1B
  python -m tools.radar_sync.sync_one_patient --cpmrn INKABEL46974 --encounter 1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Environment setup ─────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "app"))
from dotenv import load_dotenv
load_dotenv(_ROOT / "app" / ".env")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("sync_one_patient")

# ── Local imports ─────────────────────────────────────────────────────────────
from tools.radar_sync.chart_puller import get_admitted_patients, pull_chart, upsert_to_local
from tools.radar_sync.notes_module.admission_loader import load_admission
from tools.radar_sync.notes_module.mongo_cache import save_index
from tools.radar_sync.delta_extractor import extract_delta
from tools.radar_sync.summary_updater import update_summary
from tools.radar_sync.cds_runner import run_cds
from tools.radar_sync.patient_context import get_context, update_summary as ctx_update_summary, append_entry


def run(workspace: str = "1A", cpmrn: str | None = None, encounter: int = 1) -> dict:
    snapshot_at = datetime.now(timezone.utc)
    logger.info("=== SYNC START  workspace=%s  time=%s ===", workspace, snapshot_at.isoformat())

    # ── Step 1: Discover ──────────────────────────────────────────────────────
    if cpmrn:
        logger.info("Step 1: Using provided CPMRN=%s encounter=%d", cpmrn, encounter)
    else:
        logger.info("Step 1: Fetching admitted patients from workspace %s", workspace)
        patients = get_admitted_patients(workspace)
        if not patients:
            logger.error("No patients found in workspace %s", workspace)
            return {"error": "no_patients"}
        patient_info = patients[0]
        cpmrn    = patient_info["CPMRN"]
        encounter = patient_info.get("encounter", 1)
        logger.info("Step 1: Selected CPMRN=%s encounter=%d (%s)", cpmrn, encounter, patient_info.get("display_name", ""))

    # ── Step 2: Pull chart ────────────────────────────────────────────────────
    logger.info("Step 2: Pulling chart for CPMRN=%s", cpmrn)
    chart = pull_chart(cpmrn, encounter)

    # ── Step 3: Upsert to local MongoDB ───────────────────────────────────────
    logger.info("Step 3: Upserting chart to local MongoDB")
    upsert_to_local(chart)

    # ── Step 4: Rebuild FAISS note index ─────────────────────────────────────
    logger.info("Step 4: Building FAISS note index from %d notes", _count_notes(chart))
    store = load_admission(chart)
    admission_id = f"{cpmrn}_{encounter}"
    save_index(store, admission_id)
    logger.info("Step 4: Index saved (%d chunks)", len(store.text_chunks))

    # ── Step 5: Extract delta ─────────────────────────────────────────────────
    logger.info("Step 5: Extracting delta")
    ctx = get_context(cpmrn, encounter)
    last_ts = ctx.get("last_snapshot_at")
    if isinstance(last_ts, str):
        from datetime import datetime as dt
        try:
            last_ts = dt.fromisoformat(last_ts.replace("Z", "+00:00"))
        except Exception:
            last_ts = None
    delta = extract_delta(chart, last_ts)
    logger.info(
        "Step 5: Delta — %d new vitals, %d new labs, %d new notes",
        len(delta["new_vitals"]), len(delta["new_labs"]), len(delta["new_notes"]),
    )

    # ── Step 6: Update rolling summary ───────────────────────────────────────
    logger.info("Step 6: Updating rolling summary")
    existing_summary = ctx.get("running_summary", "")
    new_summary = update_summary(existing_summary, delta, cpmrn, chart=chart)
    logger.info("Step 6: Summary updated (%d chars)", len(new_summary))

    # ── Step 7: Run CDS ───────────────────────────────────────────────────────
    logger.info("Step 7: Running CDS for CPMRN=%s", cpmrn)
    cds_result = run_cds(cpmrn, encounter, new_summary, chart, delta.get("io_last_24h", {}))

    suggestions = {
        "immediate_actions":          cds_result.get("immediate_actions", []),
        "immediate_next_steps":       cds_result.get("immediate_next_steps", []),
        "clinical_reasoning":         cds_result.get("clinical_reasoning", []),
        "monitoring_followup":        cds_result.get("monitoring_followup", []),
        "alternative_considerations": cds_result.get("alternative_considerations", []),
        "structured_orders":          cds_result.get("structured_orders", []),
        "cost_usd":                   cds_result.get("cost_usd"),
    }
    logger.info("Step 7: CDS complete — %d immediate actions", len(suggestions.get("immediate_actions") or suggestions.get("immediate_next_steps") or []))

    # ── Step 8: Store ─────────────────────────────────────────────────────────
    logger.info("Step 8: Storing results")
    entry = {
        "snapshot_at":     snapshot_at.isoformat(),
        "delta_summary": {
            "new_vitals_count": len(delta["new_vitals"]),
            "new_labs_count":   len(delta["new_labs"]),
            "new_notes_count":  len(delta["new_notes"]),
        },
        "io_last_24h":     delta.get("io_last_24h", {}),
        "suggestions":     suggestions,
    }
    ctx_update_summary(cpmrn, encounter, new_summary, snapshot_at)
    append_entry(cpmrn, encounter, entry)

    result = {
        "cpmrn":          cpmrn,
        "encounter":      encounter,
        "snapshot_at":    snapshot_at.isoformat(),
        "note_chunks":    len(store.text_chunks),
        "running_summary": new_summary,
        "suggestions":    suggestions,
    }
    logger.info("=== SYNC COMPLETE CPMRN=%s ===", cpmrn)
    return result


def _count_notes(chart: dict) -> int:
    notes = (chart.get("notes") or {}).get("finalNotes") or []
    return sum(len(note.get("content") or []) for note in notes)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hourly live patient CDS sync")
    parser.add_argument("--workspace", default="1A", help="Radar workspace (default: 1A)")
    parser.add_argument("--cpmrn",     default=None,  help="Override CPMRN (skip discovery)")
    parser.add_argument("--encounter", default=1, type=int, help="Encounter number (default: 1)")
    args = parser.parse_args()

    result = run(workspace=args.workspace, cpmrn=args.cpmrn, encounter=args.encounter)
    print(json.dumps(result, indent=2, default=str))
