"""
Run Audit tab data helpers.

get_active_next_checks() — queries BigQuery for the latest next_check state per
  (CPMRN, encounter, problem_name), scoped to active / recently-run patients.
  Replaces the old GCS full-scan which was O(n_patients) serial HTTP GETs.

get_patient_detail() — single GCS blob reads for per-patient drill-down (already fast).
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Patients with no run in the last N hours are treated as inactive even if
# no discharge closing row was emitted (safety net for the BQ query).
_ACTIVE_WINDOW_HOURS = 36


def _get_gcs_db():
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[1]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    os.environ.setdefault("GCS_BUCKET", "patientview-cds-pipeline-ops")
    from backend.services.gcs_store import get_gcs_db
    return get_gcs_db()


def _get_bq_store():
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[1]
    for p in [str(_root / "app"), str(_root)]:
        if p not in sys.path:
            sys.path.insert(0, p)
    from backend.services.bq_store import get_bq_store
    return get_bq_store()


def _serialize(obj: Any) -> Any:
    """Recursively convert datetime objects to ISO strings for JSON safety."""
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_serialize(i) for i in obj]
    return obj


def get_active_next_checks() -> list[dict]:
    """
    Query BQ for the latest next_check state per (CPMRN, encounter, problem_name).

    Filters:
      - due_after IS NOT NULL   — only open windows (NULL = resolved/closed)
      - run_started_at > NOW() - 36h — patients must have run recently (discharge safety net)

    Returns a list sorted by due_after ascending.
    """
    try:
        store = _get_bq_store()
        from google.cloud import bigquery

        sql = f"""
        WITH latest AS (
          SELECT
            CPMRN, encounter, problem_name,
            nc_type, nc_key, nc_label, due_after, clinical_status,
            ROW_NUMBER() OVER (
              PARTITION BY CPMRN, encounter, problem_name
              ORDER BY run_started_at DESC
            ) AS rn
          FROM `{store._project}.{store._dataset}.patient_next_checks`
          WHERE run_started_at >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL {_ACTIVE_WINDOW_HOURS} HOUR)
        )
        SELECT CPMRN, encounter, problem_name, nc_type, nc_key, nc_label, due_after, clinical_status
        FROM latest
        WHERE rn = 1
          AND due_after IS NOT NULL
        ORDER BY due_after ASC
        """
        rows = store._query(sql)

        now = datetime.now(timezone.utc)
        results = []
        for r in rows:
            due_raw = r.get("due_after")
            if due_raw is None:
                continue
            if isinstance(due_raw, str):
                try:
                    due = datetime.fromisoformat(due_raw.replace("Z", "+00:00"))
                except ValueError:
                    continue
            elif isinstance(due_raw, datetime):
                due = due_raw
            else:
                continue
            if due.tzinfo is None:
                due = due.replace(tzinfo=timezone.utc)

            nc_key   = r.get("nc_key") or ""
            nc_label = r.get("nc_label") or nc_key or r.get("nc_type") or ""

            results.append({
                "CPMRN":           r.get("CPMRN", ""),
                "encounter":       int(r.get("encounter") or 1),
                "problem_name":    r.get("problem_name", ""),
                "type":            r.get("nc_type") or "",
                "key":             nc_key,
                "label":           nc_label,
                "due_after":       due.isoformat(),
                "overdue":         now > due,
                "clinical_status": r.get("clinical_status", ""),
            })

        return results

    except Exception:
        log.exception("audit: get_active_next_checks (BQ) failed")
        return []


def get_patient_detail(cpmrn: str, encounter: int) -> dict:
    """
    Return the latest patient context + full problem docs for the lazy drill-down.

    Reads:
      - patient_contexts/{CPMRN}_{encounter}.json  → running_summary, lightweight_summary,
                                                      structured_summary (narrative, problems,
                                                      resolved_problems, suggested_actions)
      - patient_problems/{CPMRN}_{encounter}.json  → full problem docs incl. assessments[]
                                                      (last 50 per-run reasoning entries)
    """
    try:
        db = _get_gcs_db()

        ctx: dict = db["patient_contexts"].find_one(
            {"CPMRN": cpmrn, "encounter": encounter}
        ) or {}
        ctx.pop("_id", None)

        problems: list[dict] = db["patient_problems"].find(
            {"CPMRN": cpmrn, "encounter": encounter}
        )

        sched: dict = db.snapshot_schedule.find_one(
            {"CPMRN": cpmrn, "encounter": encounter}
        ) or {}

        structured: dict = ctx.get("structured_summary") or {}

        clean_problems = []
        for p in problems:
            p = dict(p)
            p.pop("_id", None)
            clean_problems.append(_serialize(p))

        return {
            "running_summary":     ctx.get("running_summary", ""),
            "lightweight_summary": ctx.get("lightweight_summary", ""),
            "narrative":           structured.get("narrative", ""),
            "problems":            _serialize(structured.get("problems") or []),
            "resolved_problems":   _serialize(structured.get("resolved_problems") or []),
            "suggested_actions":   _serialize(structured.get("suggested_actions") or []),
            "patient_problems":    clean_problems,
            "last_delta_content":  _serialize(sched.get("last_delta_content") or {}),
            "last_llm_run_at":     _serialize(sched.get("last_llm_run_at")),
        }

    except Exception:
        log.exception("audit: get_patient_detail failed for %s enc=%d", cpmrn, encounter)
        return {}
