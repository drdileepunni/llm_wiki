"""
Study Matcher — LLM-based matching of study_alerts ↔ study_sbar_import / study_task_import.

Five jobs per hourly run:

A. LLM matching (SBARs)
   For each pending SBAR, find study_alerts for the same patient within the
   time window. Call Gemini to decide if they refer to the same clinical problem.
   Confidence ≥ 0.70 → matched.

B. Window closure (SBARs)
   SBARs past window_expires_at with no match → confirmed_fn.

C. FP candidacy
   Alerts older than 2h with no match → fp_candidate (queued for adjudication).

D. LLM matching (Tasks)
   Same as A but for study_task_import. Uses the task description as the
   clinical issue text. Deduplication: tasks that overlap in time with an
   already-matched or confirmed_fn SBAR for the same patient are marked
   deduplicated instead of generating a separate FN event.

E. Window closure (Tasks)
   Tasks past window_expires_at with no match → confirmed_fn (if monitored)
   or excluded_downtime (if not). Same deduplication check applied before
   marking confirmed_fn.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_MATCH_MODEL          = "gemini-3.1-flash-lite"
_MATCH_THRESHOLD      = 0.70   # minimum confidence to confirm a match
_FP_CANDIDATE_DELAY_H = 2      # hours after alert before it becomes fp_candidate
_ALERT_WINDOW_BEFORE  = 6      # hours before SBAR creation an alert can match
_ALERT_WINDOW_AFTER   = 2      # hours after SBAR creation an alert can still match

_SYSTEM_PROMPT = (
    "You are a clinical informaticist. Determine if an ICU problem tracker alert "
    "and a high-urgency SBAR notification refer to the same clinical problem for "
    "the same patient. Answer with JSON only."
)


class _MatchResult(BaseModel):
    related:    bool
    confidence: float
    reasoning:  str


def _llm_match(problem_name: str, alert_reason: str, sbar_issues: str) -> _MatchResult:
    """Call Gemini to determine if an alert and a SBAR are about the same problem."""
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_MATCH_MODEL)

    prompt = (
        f'Alert — Problem: "{problem_name}". Reason: "{alert_reason}"\n'
        f'SBAR — Issues: "{sbar_issues}"\n\n'
        f'Respond: {{"related": true|false, "confidence": 0.0-1.0, "reasoning": "one sentence"}}'
    )

    result = client.generate_json(
        prompt=prompt,
        schema=_MatchResult,
        system=_SYSTEM_PROMPT,
        max_tokens=1024,
    )
    return _MatchResult(**result)


def _has_overlapping_sbar_event(
    cpmrn: str,
    encounter: int,
    anchor_dt: datetime,
    bq_store: Any,
) -> bool:
    """
    Return True if a matched or confirmed_fn SBAR exists for the same patient
    within the task's match window. Used to deduplicate task-based FNs that
    are already captured by a SBAR for the same deterioration event.
    """
    window_start = anchor_dt - timedelta(hours=_ALERT_WINDOW_BEFORE)
    window_end   = anchor_dt + timedelta(hours=_ALERT_WINDOW_AFTER)
    return bq_store.count_sbars({
        "CPMRN":            cpmrn,
        "encounter":        encounter,
        "match_status":     {"$in": ["matched", "confirmed_fn", "fn_reviewed_miss"]},
        "create_date_time": {"$gte": window_start, "$lte": window_end},
    }) > 0


def run_llm_matching(db: Any) -> dict:
    """
    Run all five matching jobs. Returns a summary dict for the scheduler log.
    db is the GCS-backed database (used only for db["snapshots"] monitoring checks).
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from backend.services.bq_store import get_bq_store
    bq_store = get_bq_store()

    now = datetime.now(timezone.utc)

    matched_count      = 0
    fn_count           = 0
    fp_cand_count      = 0
    llm_calls          = 0
    llm_errors         = 0
    task_matched_count = 0
    task_fn_count      = 0
    task_dedup_count   = 0

    # ── A. LLM matching ───────────────────────────────────────────────────────
    pending_sbars = bq_store.find_sbars({"match_status": "pending"})
    logger.info("study_matcher: %d pending SBARs to match", len(pending_sbars))

    for sbar in pending_sbars:
        cpmrn    = sbar.get("CPMRN", "")
        encounter = sbar.get("encounter", 1)
        create_dt = sbar.get("create_date_time")
        if isinstance(create_dt, datetime) and create_dt.tzinfo is None:
            create_dt = create_dt.replace(tzinfo=timezone.utc)
        if not create_dt:
            continue

        window_start = create_dt - timedelta(hours=_ALERT_WINDOW_BEFORE)
        window_end   = create_dt + timedelta(hours=_ALERT_WINDOW_AFTER)

        candidates = bq_store.find_alerts({
            "CPMRN":        cpmrn,
            "encounter":    encounter,
            "match_status": {"$in": ["pending", "fp_candidate"]},
            "alerted_at":   {"$gte": window_start, "$lte": window_end},
        })

        if not candidates:
            continue

        best_match     = None
        best_conf      = 0.0
        best_reasoning = ""

        for alert in candidates:
            try:
                result = _llm_match(
                    problem_name=alert.get("problem_name", ""),
                    alert_reason=alert.get("alert_reason", ""),
                    sbar_issues=sbar.get("issues", ""),
                )
                llm_calls += 1
                logger.debug(
                    "study_matcher: SBAR %s ↔ alert %s — related=%s conf=%.2f",
                    sbar["sbar_id"], alert["alert_id"], result.related, result.confidence,
                )
                if result.related and result.confidence > best_conf:
                    best_conf      = result.confidence
                    best_match     = alert
                    best_reasoning = result.reasoning
            except Exception:
                llm_errors += 1
                logger.exception(
                    "study_matcher: LLM call failed for SBAR %s alert %s",
                    sbar.get("sbar_id"), alert.get("alert_id"),
                )

        if best_match and best_conf >= _MATCH_THRESHOLD:
            bq_store.update_sbar(sbar["sbar_id"], {
                "match_status":         "matched",
                "matched_alert_id":     best_match["alert_id"],
                "llm_match_confidence": best_conf,
                "llm_match_reasoning":  best_reasoning,
                "matched_at":           now,
            })
            bq_store.update_alert(best_match["alert_id"], {
                "match_status":         "matched",
                "matched_sbar_id":      sbar["sbar_id"],
                "llm_match_confidence": best_conf,
                "llm_match_reasoning":  best_reasoning,
            })
            matched_count += 1
            logger.info(
                "study_matcher: MATCHED SBAR %s ↔ alert %s (conf=%.2f) — %s",
                sbar["sbar_id"], best_match["alert_id"], best_conf, best_reasoning,
            )

    # ── B. Window closure → confirmed_fn or excluded_downtime ────────────────
    # An SBAR whose window expired is only a true FN if the system was actively
    # monitoring the patient during that window. If no snapshot exists for the
    # patient in [create_date_time - ALERT_WINDOW_BEFORE, window_expires_at],
    # the scheduler was down — mark excluded_downtime so the metric stays clean.
    expired_sbars = bq_store.find_sbars({
        "match_status":      "pending",
        "window_expires_at": {"$lt": now},
    })
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
            bq_store.update_sbar(sbar["sbar_id"], {
                "match_status":   "confirmed_fn",
                "confirmed_fn_at": now,
            })
            fn_count += 1
        else:
            bq_store.update_sbar(sbar["sbar_id"], {
                "match_status":     "excluded_downtime",
                "excluded_at":      now,
                "exclusion_reason": "no snapshot activity during SBAR window — system was not monitoring",
            })
            downtime_count += 1

    if fn_count:
        logger.info("study_matcher: %d SBARs confirmed as FN (window expired, patient was monitored)", fn_count)
    if downtime_count:
        logger.info("study_matcher: %d SBARs excluded as downtime (no snapshot activity during window)", downtime_count)

    # ── C. FP candidacy ───────────────────────────────────────────────────────
    fp_cutoff     = now - timedelta(hours=_FP_CANDIDATE_DELAY_H)
    fp_cand_count = bq_store.mark_fp_candidates(fp_cutoff, now)
    if fp_cand_count:
        logger.info("study_matcher: %d alerts moved to fp_candidate queue", fp_cand_count)

    # ── D. LLM matching (Tasks) ───────────────────────────────────────────────
    pending_tasks = bq_store.find_tasks({"match_status": "pending"})
    logger.info("study_matcher: %d pending tasks to match", len(pending_tasks))

    for task in pending_tasks:
        cpmrn     = task.get("CPMRN", "")
        encounter  = task.get("encounter", 1)
        visible_at = task.get("task_visible_at")
        if isinstance(visible_at, datetime) and visible_at.tzinfo is None:
            visible_at = visible_at.replace(tzinfo=timezone.utc)
        if not visible_at:
            continue

        window_start = visible_at - timedelta(hours=_ALERT_WINDOW_BEFORE)
        window_end   = visible_at + timedelta(hours=_ALERT_WINDOW_AFTER)

        candidates = bq_store.find_alerts({
            "CPMRN":        cpmrn,
            "encounter":    encounter,
            "match_status": {"$in": ["pending", "fp_candidate"]},
            "alerted_at":   {"$gte": window_start, "$lte": window_end},
        })

        if not candidates:
            continue

        best_match     = None
        best_conf      = 0.0
        best_reasoning = ""

        for alert in candidates:
            try:
                result = _llm_match(
                    problem_name=alert.get("problem_name", ""),
                    alert_reason=alert.get("alert_reason", ""),
                    sbar_issues=task.get("issues", ""),
                )
                llm_calls += 1
                logger.debug(
                    "study_matcher: task %s ↔ alert %s — related=%s conf=%.2f",
                    task["task_id"], alert["alert_id"], result.related, result.confidence,
                )
                if result.related and result.confidence > best_conf:
                    best_conf      = result.confidence
                    best_match     = alert
                    best_reasoning = result.reasoning
            except Exception:
                llm_errors += 1
                logger.exception(
                    "study_matcher: LLM call failed for task %s alert %s",
                    task.get("task_id"), alert.get("alert_id"),
                )

        if best_match and best_conf >= _MATCH_THRESHOLD:
            bq_store.update_task(task["task_id"], {
                "match_status":         "matched",
                "matched_alert_id":     best_match["alert_id"],
                "llm_match_confidence": best_conf,
                "llm_match_reasoning":  best_reasoning,
                "matched_at":           now,
            })
            bq_store.update_alert(best_match["alert_id"], {
                "match_status":         "matched",
                "matched_task_id":      task["task_id"],
                "llm_match_confidence": best_conf,
                "llm_match_reasoning":  best_reasoning,
            })
            task_matched_count += 1
            logger.info(
                "study_matcher: MATCHED task %s ↔ alert %s (conf=%.2f) — %s",
                task["task_id"], best_match["alert_id"], best_conf, best_reasoning,
            )

    # ── E. Window closure (Tasks) → confirmed_fn / deduplicated / excluded_downtime ──
    expired_tasks = bq_store.find_tasks({
        "match_status":      "pending",
        "window_expires_at": {"$lt": now},
    })
    for task in expired_tasks:
        cpmrn     = task.get("CPMRN", "")
        encounter  = task.get("encounter", 1)
        visible_at = task.get("task_visible_at")
        if isinstance(visible_at, datetime) and visible_at.tzinfo is None:
            visible_at = visible_at.replace(tzinfo=timezone.utc)
        win_expires = task.get("window_expires_at")
        if isinstance(win_expires, datetime) and win_expires.tzinfo is None:
            win_expires = win_expires.replace(tzinfo=timezone.utc)

        if visible_at and _has_overlapping_sbar_event(cpmrn, encounter, visible_at, bq_store):
            bq_store.update_task(task["task_id"], {
                "match_status":    "deduplicated",
                "deduplicated_at": now,
                "dedup_reason":    "overlapping SBAR already matched or confirmed_fn",
            })
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
            bq_store.update_task(task["task_id"], {
                "match_status":    "confirmed_fn",
                "confirmed_fn_at": now,
            })
            task_fn_count += 1
        else:
            bq_store.update_task(task["task_id"], {
                "match_status":     "excluded_downtime",
                "excluded_at":      now,
                "exclusion_reason": "no snapshot activity during task window — system was not monitoring",
            })

    if task_fn_count:
        logger.info("study_matcher: %d tasks confirmed as FN (window expired, monitored, no SBAR overlap)", task_fn_count)
    if task_dedup_count:
        logger.info("study_matcher: %d tasks deduplicated (overlapping SBAR already accounts for event)", task_dedup_count)

    return {
        "matching":          "ok",
        "matched":           matched_count,
        "confirmed_fn":      fn_count,
        "fp_candidates":     fp_cand_count,
        "llm_calls":         llm_calls,
        "llm_errors":        llm_errors,
        "task_matched":      task_matched_count,
        "task_confirmed_fn": task_fn_count,
        "task_deduplicated": task_dedup_count,
    }
