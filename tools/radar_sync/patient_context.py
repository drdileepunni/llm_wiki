"""Read / write patient_contexts collection in MongoDB."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def _col():
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "app"))
    from backend.services.emr.db import get_db
    return get_db()["patient_contexts"]


def get_context(cpmrn: str, encounter: int = 1) -> dict:
    """Return the patient_context doc, or a fresh empty one if it doesn't exist."""
    doc = _col().find_one({"CPMRN": cpmrn, "encounter": encounter})
    if doc:
        doc.pop("_id", None)
        return doc
    return {
        "CPMRN": cpmrn,
        "encounter": encounter,
        "running_summary": "",
        "last_snapshot_at": None,
        "hourly_entries": [],
    }


def save_context(ctx: dict) -> None:
    """Upsert the patient_context doc."""
    cpmrn    = ctx["CPMRN"]
    encounter = ctx.get("encounter", 1)
    _col().update_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"$set": {
            "running_summary":   ctx.get("running_summary", ""),
            "last_snapshot_at":  ctx.get("last_snapshot_at"),
        },
         "$push": {"hourly_entries": {"$each": ctx.get("_new_entries", [])}}},
        upsert=True,
    )
    logger.info("save_context: updated context for CPMRN=%s enc=%s", cpmrn, encounter)


def append_entry(cpmrn: str, encounter: int, entry: dict) -> None:
    """Append a single hourly_entry to the patient_context."""
    _col().update_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"$push": {"hourly_entries": entry},
         "$set":  {"last_snapshot_at": entry.get("snapshot_at")}},
        upsert=True,
    )


def update_summary(cpmrn: str, encounter: int, summary: str, snapshot_at: datetime) -> None:
    """Update running_summary and last_snapshot_at."""
    _col().update_one(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"$set": {
            "running_summary":  summary,
            "last_snapshot_at": snapshot_at,
        }},
        upsert=True,
    )
