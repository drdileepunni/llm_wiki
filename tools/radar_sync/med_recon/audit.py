"""
Thin audit writers over BQStudyStore for the med-recon trace.

`write_recon_audit` records the full per-run trace (document → transcription →
comparison → proposal). `write_recon_action_result` records one row per applied
action on Submit (who/what/result). Both are best-effort: a BQ failure must never
break reconciliation or the Submit response.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def write_recon_audit(doc: dict) -> str:
    """Write a med_recon_audit row; returns recon_id (or doc's recon_id on failure)."""
    try:
        from app.backend.services.bq_store import get_bq_store
        return get_bq_store().insert_med_recon(doc)
    except Exception:
        logger.exception("write_recon_audit: failed for recon_id=%s", doc.get("recon_id"))
        return doc.get("recon_id", "")


def write_recon_action_result(doc: dict) -> None:
    """Write one med_recon_actions row (best-effort)."""
    try:
        from app.backend.services.bq_store import get_bq_store
        get_bq_store().insert_med_recon_action(doc)
    except Exception:
        logger.exception("write_recon_action_result: failed for recon_id=%s", doc.get("recon_id"))
