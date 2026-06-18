"""
Thin audit writers over BQStudyStore for the diagnostic-report-interpretation trace.

`write_report_audit`    — one row per interpreted report (report → description →
                          interpretation → findings → card status).
`write_report_run`      — one row per patient per cycle, written on EVERY path
                          (processed / no_new_docs / disabled / skipped_cycle_limit /
                          send_failed / error) so the cycle is reconstructable from BQ.
`write_report_feedback` — one row per clinician rating on Submit.

All best-effort: a BQ failure must never break the pipeline or the Submit response.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def write_report_audit(doc: dict) -> str:
    """Write a report_interpret_audit row; returns report_id (or doc's report_id on failure)."""
    try:
        from app.backend.services.bq_store import get_bq_store
        return get_bq_store().insert_report_interpret(doc)
    except Exception:
        logger.exception("write_report_audit: failed for report_id=%s", doc.get("report_id"))
        return doc.get("report_id", "")


def write_report_run(doc: dict) -> None:
    """Write a report_interpret_runs row (best-effort)."""
    try:
        from app.backend.services.bq_store import get_bq_store
        get_bq_store().insert_report_run(doc)
    except Exception:
        logger.exception("write_report_run: failed for run_id=%s", doc.get("run_id"))


def write_report_feedback(doc: dict) -> None:
    """Write a report_interpret_feedback row (best-effort)."""
    try:
        from app.backend.services.bq_store import get_bq_store
        get_bq_store().insert_report_feedback(doc)
    except Exception:
        logger.exception("write_report_feedback: failed for report_id=%s", doc.get("report_id"))
