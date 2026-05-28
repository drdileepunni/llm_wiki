"""
Study Matcher — matches study_alerts ↔ study_sbar_import / study_task_import.

Three jobs per hourly run:

B. Window closure (SBARs)
   SBARs past window_expires_at with no match → confirmed_fn (if monitored)
   or excluded_downtime (if system was down during the window).

C. FP candidacy
   Alerts older than 2h with no match → fp_candidate (queued for human adjudication).

E. Window closure (Tasks)
   Tasks past window_expires_at with no match → confirmed_fn (if monitored)
   or excluded_downtime (if not). Deduplication: tasks that overlap in time with an
   already-matched or confirmed_fn SBAR for the same patient are marked
   deduplicated instead of generating a separate FN event.

TPs are determined exclusively by human adjudication via the review UI
(match_status = "tp_confirmed"). LLM-based matching has been removed.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_FP_CANDIDATE_DELAY_H = 2      # hours after alert before it becomes fp_candidate
_ALERT_WINDOW_BEFORE  = 6      # hours before SBAR creation an alert can match
_ALERT_WINDOW_AFTER   = 2      # hours after SBAR creation an alert can still match


def _has_overlapping_sbar_event(
    cpmrn: str,
    encounter: int,
    anchor_dt: datetime,
    sbars_col: Any,
) -> bool:
    """
    Return True if a confirmed_fn SBAR exists for the same patient within the
    task's match window. Used to deduplicate task-based FNs already captured
    by a SBAR for the same deterioration event.
    """
    window_start = anchor_dt - timedelta(hours=_ALERT_WINDOW_BEFORE)
    window_end   = anchor_dt + timedelta(hours=_ALERT_WINDOW_AFTER)
    return sbars_col.count_documents({
        "CPMRN":            cpmrn,
        "encounter":        encounter,
        "match_status":     {"$in": ["confirmed_fn", "fn_reviewed_miss"]},
        "create_date_time": {"$gte": window_start, "$lte": window_end},
    }) > 0


def run_llm_matching(db: Any) -> dict:
    """
    Run all matching jobs. Returns a summary dict for the scheduler log.
    """
    now = datetime.now(timezone.utc)
    alerts_col = db["study_alerts"]
    sbars_col  = db["study_sbar_import"]
    tasks_col  = db["study_task_import"]

    fn_count           = 0
    fp_cand_count      = 0
    task_fn_count      = 0
    task_dedup_count   = 0

    # ── B. Window closure → confirmed_fn or excluded_downtime ────────────────
    # An SBAR whose window expired is only a true FN if the system was actively
    # monitoring the patient during that window. If no snapshot exists for the
    # patient in [create_date_time - ALERT_WINDOW_BEFORE, window_expires_at],
    # the scheduler was down — mark excluded_downtime so the metric stays clean.
    expired_sbars = list(sbars_col.find({
        "match_status":      "pending",
        "window_expires_at": {"$lt": now},
    }))
    downtime_count = 0
    for sbar in expired_sbars:
        cpmrn    = sbar.get("CPMRN", "")
        encounter = sbar.get("encounter", 1)
        create_dt = sbar.get("create_date_time")
        if isinstance(create_dt, datetime) and create_dt.tzinfo is None:
            create_dt = create_dt.replace(tzinfo=timezone.utc)
        win_expires = sbar.get("window_expires_at")
        if isinstance(win_expires, datetime) and win_expires.tzinfo is None:
            win_expires = win_expires.replace(tzinfo=timezone.utc)

        monitor_start = (create_dt - timedelta(hours=_ALERT_WINDOW_BEFORE)) if create_dt else None
        was_monitored = False
        if monitor_start and win_expires and cpmrn:
            was_monitored = db["snapshots"].count_documents({
                "CPMRN":       cpmrn,
                "encounter":   encounter,
                "snapshot_at": {"$gte": monitor_start, "$lte": win_expires},
            }) > 0

        if was_monitored:
            sbars_col.update_one(
                {"_id": sbar["_id"]},
                {"$set": {"match_status": "confirmed_fn", "confirmed_fn_at": now}},
            )
            fn_count += 1
        else:
            sbars_col.update_one(
                {"_id": sbar["_id"]},
                {"$set": {
                    "match_status":       "excluded_downtime",
                    "excluded_at":        now,
                    "exclusion_reason":   "no snapshot activity during SBAR window — system was not monitoring",
                }},
            )
            downtime_count += 1

    if fn_count:
        logger.info("study_matcher: %d SBARs confirmed as FN (window expired, patient was monitored)", fn_count)
    if downtime_count:
        logger.info("study_matcher: %d SBARs excluded as downtime (no snapshot activity during window)", downtime_count)

    # ── C. FP candidacy ───────────────────────────────────────────────────────
    fp_cutoff = now - timedelta(hours=_FP_CANDIDATE_DELAY_H)
    fp_update = alerts_col.update_many(
        {
            "match_status": "pending",
            "alerted_at":   {"$lt": fp_cutoff},
        },
        {"$set": {"match_status": "fp_candidate", "fp_candidate_at": now}},
    )
    fp_cand_count = fp_update.modified_count
    if fp_cand_count:
        logger.info("study_matcher: %d alerts moved to fp_candidate queue", fp_cand_count)

    # ── E. Window closure (Tasks) → confirmed_fn / deduplicated / excluded_downtime ──
    expired_tasks = list(tasks_col.find({
        "match_status":      "pending",
        "window_expires_at": {"$lt": now},
    }))
    for task in expired_tasks:
        cpmrn     = task.get("CPMRN", "")
        encounter  = task.get("encounter", 1)
        visible_at = task.get("task_visible_at")
        if isinstance(visible_at, datetime) and visible_at.tzinfo is None:
            visible_at = visible_at.replace(tzinfo=timezone.utc)
        win_expires = task.get("window_expires_at")
        if isinstance(win_expires, datetime) and win_expires.tzinfo is None:
            win_expires = win_expires.replace(tzinfo=timezone.utc)

        # Deduplication: skip if a SBAR already captures this event
        if visible_at and _has_overlapping_sbar_event(cpmrn, encounter, visible_at, sbars_col):
            tasks_col.update_one(
                {"_id": task["_id"]},
                {"$set": {
                    "match_status":       "deduplicated",
                    "deduplicated_at":    now,
                    "dedup_reason":       "overlapping SBAR already confirmed_fn",
                }},
            )
            task_dedup_count += 1
            continue

        monitor_start = (visible_at - timedelta(hours=_ALERT_WINDOW_BEFORE)) if visible_at else None
        was_monitored = False
        if monitor_start and win_expires and cpmrn:
            was_monitored = db["snapshots"].count_documents({
                "CPMRN":       cpmrn,
                "encounter":   encounter,
                "snapshot_at": {"$gte": monitor_start, "$lte": win_expires},
            }) > 0

        if was_monitored:
            tasks_col.update_one(
                {"_id": task["_id"]},
                {"$set": {"match_status": "confirmed_fn", "confirmed_fn_at": now}},
            )
            task_fn_count += 1
        else:
            tasks_col.update_one(
                {"_id": task["_id"]},
                {"$set": {
                    "match_status":     "excluded_downtime",
                    "excluded_at":      now,
                    "exclusion_reason": "no snapshot activity during task window — system was not monitoring",
                }},
            )

    if task_fn_count:
        logger.info("study_matcher: %d tasks confirmed as FN (window expired, monitored, no SBAR overlap)", task_fn_count)
    if task_dedup_count:
        logger.info("study_matcher: %d tasks deduplicated (overlapping SBAR already accounts for event)", task_dedup_count)

    return {
        "matching":          "ok",
        "confirmed_fn":      fn_count,
        "fp_candidates":     fp_cand_count,
        "task_confirmed_fn": task_fn_count,
        "task_deduplicated": task_dedup_count,
    }
