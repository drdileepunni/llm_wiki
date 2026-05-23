"""
Study SBAR Syncer — pulls High-urgency SBARs from BigQuery into MongoDB.

Runs hourly as part of the study pipeline. Fetches the last 8 hours of
High-urgency SBARs (documents/vitals/summary/intake-output modules only)
and upserts them into study_sbar_import. Sets window_expires_at = 8h after
SBAR creation, after which unmatched SBARs become confirmed false negatives.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_LOOKBACK_HOURS   = 8   # how far back to pull SBARs each run
_WINDOW_HOURS     = 8   # how long after SBAR creation an alert can still match
_BQ_PROJECT       = "prod-tech-project1-bv479-zo027"
_BQ_TABLE         = f"{_BQ_PROJECT}.patient.latest_sbar_fact"
_RELEVANT_MODULES = ("documents", "vitals", "summary", "intake-output")

_QUERY = f"""
SELECT
  sbar_id,
  cpmrn,
  CAST(encounters AS STRING) AS encounter_str,
  hospital_name,
  unit_name,
  urgency,
  issues,
  module,
  create_date_time,
  is_reviewed,
  reviewer_name,
  action
FROM `{_BQ_TABLE}`
WHERE urgency = 'High'
  AND module IN ({", ".join(f"'{m}'" for m in _RELEVANT_MODULES)})
  AND create_date_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_LOOKBACK_HOURS} HOUR)
"""


def _get_bq_client():
    """Return a BigQuery client using Application Default Credentials."""
    from google.cloud import bigquery
    return bigquery.Client(project=_BQ_PROJECT)


def sync_sbars(db: Any) -> dict:
    """
    Pull High-urgency SBARs from BigQuery and upsert into study_sbar_import.
    Returns a summary dict for the scheduler log.
    """
    col = db["study_sbar_import"]
    now = datetime.now(timezone.utc)

    try:
        bq = _get_bq_client()
        rows = list(bq.query(_QUERY).result())
    except Exception:
        logger.exception("study_sbar_syncer: BigQuery query failed")
        return {"sbar_sync": "error", "upserted": 0, "skipped": 0}

    upserted = skipped = 0
    for row in rows:
        sbar_id = row["sbar_id"]
        if not sbar_id:
            skipped += 1
            continue

        # Parse encounter — BigQuery stores it as STRING "1", "2", etc.
        try:
            encounter = int(row["encounter_str"] or 1)
        except (TypeError, ValueError):
            encounter = 1

        create_dt = row["create_date_time"]
        if isinstance(create_dt, datetime) and create_dt.tzinfo is None:
            create_dt = create_dt.replace(tzinfo=timezone.utc)

        window_expires_at = create_dt + timedelta(hours=_WINDOW_HOURS) if create_dt else now + timedelta(hours=_WINDOW_HOURS)

        doc = {
            "sbar_id":          sbar_id,
            "CPMRN":            row["cpmrn"] or "",
            "encounter":        encounter,
            "hospital_name":    row["hospital_name"] or "",
            "unit_name":        row["unit_name"] or "",
            "urgency":          row["urgency"] or "High",
            "issues":           row["issues"] or "",
            "module":           row["module"] or "",
            "create_date_time": create_dt,
            "is_reviewed":      row["is_reviewed"] or False,
            "reviewer_name":    row["reviewer_name"] or "",
            "action":           row["action"] or "",
            "window_expires_at": window_expires_at,
        }

        result = col.update_one(
            {"sbar_id": sbar_id},
            {
                "$setOnInsert": {
                    **doc,
                    "match_status":    "pending",
                    "matched_alert_id": None,
                    "synced_at":       now,
                }
            },
            upsert=True,
        )
        if result.upserted_id:
            upserted += 1
            logger.debug("study_sbar_syncer: new SBAR %s CPMRN=%s", sbar_id, doc["CPMRN"])
        else:
            skipped += 1  # already exists — don't overwrite match_status

    logger.info(
        "study_sbar_syncer: %d new SBARs upserted, %d already present",
        upserted, skipped,
    )
    return {"sbar_sync": "ok", "upserted": upserted, "skipped": skipped}
