"""
Study Runner — orchestrates the hourly study pipeline.

Called from scheduler._collect_all() after the per-patient loop.
Runs three sub-jobs in order: SBAR sync → LLM matching → metrics compute.
Each sub-job is independent and failures are caught individually.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_study_jobs(db: Any) -> dict:
    """
    Run the full hourly study pipeline. Returns a combined status dict.
    Each sub-job failure is logged but does not abort subsequent jobs.
    """
    result: dict = {}

    # 1. Sync High-urgency SBARs from BigQuery
    try:
        from tools.radar_sync.study_sbar_syncer import sync_sbars
        result["sbar_sync"] = sync_sbars(db)
        logger.info("study_runner: SBAR sync done — %s", result["sbar_sync"])
    except Exception:
        logger.exception("study_runner: SBAR sync failed")
        result["sbar_sync"] = {"sbar_sync": "error"}

    # 1b. Sync abnormal-vital escalation tasks from BigQuery
    try:
        from tools.radar_sync.study_task_syncer import sync_tasks
        result["task_sync"] = sync_tasks(db)
        logger.info("study_runner: task sync done — %s", result["task_sync"])
    except Exception:
        logger.exception("study_runner: task sync failed")
        result["task_sync"] = {"task_sync": "error"}

    # 2. LLM matching + window closure + FP candidacy
    try:
        from tools.radar_sync.study_matcher import run_llm_matching
        result["matching"] = run_llm_matching(db)
        logger.info("study_runner: matching done — %s", result["matching"])
    except Exception:
        logger.exception("study_runner: matching failed")
        result["matching"] = {"matching": "error"}

    # 3. Rolling metrics computation
    try:
        from tools.radar_sync.study_metrics import compute_metrics
        result["metrics"] = compute_metrics(db)
        logger.info("study_runner: metrics done — %s", result["metrics"])
    except Exception:
        logger.exception("study_runner: metrics failed")
        result["metrics"] = {"metrics": "error"}

    return result
