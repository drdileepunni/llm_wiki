"""
Study Task Syncer — pulls abnormal-vital escalation tasks from BigQuery into MongoDB.

Runs hourly alongside the SBAR syncer. Fetches non-recurring "Review Abnormal Vitals"
tasks from the last 8 hours and upserts them into study_task_import. Sets
window_expires_at = 8h after task_visible_at, after which unmatched tasks become
confirmed false negatives (subject to deduplication against SBARs for the same event).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_LOOKBACK_HOURS = 8
_WINDOW_HOURS   = 8
_BQ_PROJECT     = "prod-tech-project1-bv479-zo027"
_BQ_TABLE       = f"{_BQ_PROJECT}.analytics.tasks_fact"

_QUERY = f"""
SELECT
  task_id,
  cpmrn,
  CAST(encounters AS STRING) AS encounter_str,
  hospital_name,
  unit_name,
  title,
  description,
  priority,
  status,
  is_recurring,
  task_visible_at,
  completed_at
FROM `{_BQ_TABLE}`
WHERE title = 'Review Abnormal Vitals'
  AND is_recurring = false
  AND task_visible_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_LOOKBACK_HOURS} HOUR)
"""


def sync_tasks(db: Any) -> dict:
    """
    Pull abnormal-vital escalation tasks from BigQuery and upsert into study_task_import.
    Returns a summary dict for the scheduler log.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from backend.services.bq_client import get_bq_client, parse_bq_dt

    col = db["study_task_import"]
    now = datetime.now(timezone.utc)

    try:
        rows = get_bq_client().execute_select(_QUERY)
    except Exception:
        logger.exception("study_task_syncer: BigQuery query failed")
        return {"task_sync": "error", "upserted": 0, "skipped": 0}

    upserted = skipped = 0
    for row in rows:
        task_id = row["task_id"]
        if not task_id:
            skipped += 1
            continue

        try:
            encounter = int(row["encounter_str"] or 1)
        except (TypeError, ValueError):
            encounter = 1

        visible_at = parse_bq_dt(row["task_visible_at"])

        window_expires_at = (
            visible_at + timedelta(hours=_WINDOW_HOURS)
            if visible_at
            else now + timedelta(hours=_WINDOW_HOURS)
        )

        doc = {
            "task_id":          task_id,
            "CPMRN":            row["cpmrn"] or "",
            "encounter":        encounter,
            "hospital_name":    row["hospital_name"] or "",
            "unit_name":        row["unit_name"] or "",
            "title":            row["title"] or "",
            "issues":           row["description"] or "",  # maps to SBAR issues for LLM matching
            "priority":         row["priority"] or "",
            "status":           row["status"] or "",
            "task_visible_at":  visible_at,
            "window_expires_at": window_expires_at,
        }

        result = col.update_one(
            {"task_id": task_id},
            {
                "$setOnInsert": {
                    **doc,
                    "match_status":     "pending",
                    "matched_alert_id": None,
                    "synced_at":        now,
                }
            },
            upsert=True,
        )
        if result.upserted_id:
            upserted += 1
            logger.debug("study_task_syncer: new task %s CPMRN=%s", task_id, doc["CPMRN"])
        else:
            skipped += 1  # already exists — don't overwrite match_status

    logger.info(
        "study_task_syncer: %d new tasks upserted, %d already present",
        upserted, skipped,
    )
    return {"task_sync": "ok", "upserted": upserted, "skipped": skipped}
