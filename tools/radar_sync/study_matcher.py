"""
Study Matcher — LLM-based matching of study_alerts ↔ study_sbar_import.

Three jobs per hourly run:

A. LLM matching
   For each pending SBAR, find study_alerts for the same patient within the
   time window. Call Gemini to decide if they refer to the same clinical problem.
   Confidence ≥ 0.70 → matched.

B. Window closure
   SBARs past window_expires_at with no match → confirmed_fn.

C. FP candidacy
   Alerts older than 2h with no match → fp_candidate (queued for adjudication).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)

_MATCH_MODEL          = "gemini-2.5-flash"
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
        max_tokens=256,
    )
    return _MatchResult(**result)


def run_llm_matching(db: Any) -> dict:
    """
    Run all three matching jobs. Returns a summary dict for the scheduler log.
    """
    now = datetime.now(timezone.utc)
    alerts_col = db["study_alerts"]
    sbars_col  = db["study_sbar_import"]

    matched_count    = 0
    fn_count         = 0
    fp_cand_count    = 0
    llm_calls        = 0
    llm_errors       = 0

    # ── A. LLM matching ───────────────────────────────────────────────────────
    pending_sbars = list(sbars_col.find({"match_status": "pending"}))
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

        # Find candidate alerts for this patient in the time window
        candidates = list(alerts_col.find({
            "CPMRN":       cpmrn,
            "encounter":   encounter,
            "match_status": {"$in": ["pending", "fp_candidate"]},
            "alerted_at":  {"$gte": window_start, "$lte": window_end},
        }))

        if not candidates:
            continue  # no candidates — leave pending, may match a future alert

        best_match    = None
        best_conf     = 0.0
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
                    sbar["sbar_id"], alert["_id"], result.related, result.confidence,
                )
                if result.related and result.confidence > best_conf:
                    best_conf      = result.confidence
                    best_match     = alert
                    best_reasoning = result.reasoning
            except Exception:
                llm_errors += 1
                logger.exception(
                    "study_matcher: LLM call failed for SBAR %s alert %s",
                    sbar.get("sbar_id"), alert.get("_id"),
                )

        if best_match and best_conf >= _MATCH_THRESHOLD:
            # Mark SBAR as matched
            sbars_col.update_one(
                {"_id": sbar["_id"]},
                {"$set": {
                    "match_status":         "matched",
                    "matched_alert_id":     str(best_match["_id"]),
                    "llm_match_confidence": best_conf,
                    "llm_match_reasoning":  best_reasoning,
                    "matched_at":           now,
                }},
            )
            # Mark alert as matched
            alerts_col.update_one(
                {"_id": best_match["_id"]},
                {"$set": {
                    "match_status":         "matched",
                    "matched_sbar_id":      sbar["sbar_id"],
                    "llm_match_confidence": best_conf,
                    "llm_match_reasoning":  best_reasoning,
                }},
            )
            matched_count += 1
            logger.info(
                "study_matcher: MATCHED SBAR %s ↔ alert %s (conf=%.2f) — %s",
                sbar["sbar_id"], best_match["_id"], best_conf, best_reasoning,
            )

    # ── B. Window closure → confirmed_fn ─────────────────────────────────────
    expired = sbars_col.update_many(
        {
            "match_status":      "pending",
            "window_expires_at": {"$lt": now},
        },
        {"$set": {"match_status": "confirmed_fn", "confirmed_fn_at": now}},
    )
    fn_count = expired.modified_count
    if fn_count:
        logger.info("study_matcher: %d SBARs confirmed as FN (window expired)", fn_count)

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

    return {
        "matching":       "ok",
        "matched":        matched_count,
        "confirmed_fn":   fn_count,
        "fp_candidates":  fp_cand_count,
        "llm_calls":      llm_calls,
        "llm_errors":     llm_errors,
    }
