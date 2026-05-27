"""
Study Metrics Calculator — computes rolling TP/FP/FN/TN and derived statistics.

Runs hourly after the matcher. Reads study_alerts, study_sbar_import,
study_task_import, study_adjudications, and snapshots. Writes one doc to
study_metrics_snapshots (BigQuery).

2×2 definitions:
  TP = study_alerts where match_status IN ("matched", "tp_confirmed")
  FP = study_alerts where match_status = "fp_confirmed"  (adjudicated Inappropriate)
  FN = (study_sbar_import confirmed_fn) + (study_task_import confirmed_fn)
       deduplicated tasks are excluded from the FN count
  TN = total patient-hours monitored  −  (TP + FP + FN)

Secondary metrics:
  - Lead time: alerted_at − create_date_time for matched TPs (negative = early warning)
  - Suppression rate: study_suppressed_events that had a matching SBAR within window
  - Explainability: distribution of explainability_rating in study_adjudications
"""
from __future__ import annotations

import logging
import statistics
from datetime import datetime, timezone, timedelta
from typing import Any

# Snapshots before this timestamp are excluded from the TN denominator.
# Study formally started May 26 2026 07:00 EST = 11:00 UTC.
STUDY_START_UTC = datetime(2026, 5, 26, 11, 0, 0, tzinfo=timezone.utc)

logger = logging.getLogger(__name__)

_MATCH_WINDOW_BEFORE = 6   # hours — must match study_matcher._ALERT_WINDOW_BEFORE
_MATCH_WINDOW_AFTER  = 2   # hours — must match study_matcher._ALERT_WINDOW_AFTER


def _wilson_ci(count: int, total: int) -> tuple[float, float]:
    """Wilson score 95% confidence interval. Returns (low, high) or (0, 0) if total=0."""
    if total == 0:
        return (0.0, 0.0)
    from math import sqrt
    z = 1.96
    p = count / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    margin = (z * sqrt(p * (1 - p) / total + z * z / (4 * total * total))) / denom
    return (max(0.0, round(centre - margin, 4)), min(1.0, round(centre + margin, 4)))


def compute_metrics(db: Any, start_dt: datetime = STUDY_START_UTC, end_dt: datetime | None = None) -> dict:
    """Compute rolling study metrics and write a snapshot. Returns summary dict.
    db is the GCS database, used only for patient snapshots (hourly monitoring TN count).
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from backend.services.bq_store import get_bq_store
    bq_store = get_bq_store()

    now          = datetime.now(timezone.utc)
    effective_end = end_dt if end_dt else now

    alert_time_q = {"alerted_at":        {"$gte": start_dt, "$lte": effective_end}}
    sbar_time_q  = {"create_date_time":  {"$gte": start_dt, "$lte": effective_end}}
    task_time_q  = {"task_visible_at":   {"$gte": start_dt, "$lte": effective_end}}
    snap_time_q  = {"snapshot_at":       {"$gte": start_dt, "$lte": effective_end}}

    # ── TP / FP / FN counts ───────────────────────────────────────────────────
    tp = bq_store.count_alerts({"match_status": "tp_confirmed", **alert_time_q})
    fp = bq_store.count_alerts({"match_status": "fp_confirmed", **alert_time_q})

    _fn_statuses = ["confirmed_fn", "fn_reviewed_miss"]
    fn_sbar = bq_store.count_sbars({"match_status": {"$in": _fn_statuses}, **sbar_time_q})
    fn_task = bq_store.count_tasks({"match_status": {"$in": _fn_statuses}, **task_time_q})
    fn = fn_sbar + fn_task
    excluded_downtime      = bq_store.count_sbars({"match_status": "excluded_downtime"})
    task_excluded_downtime = bq_store.count_tasks({"match_status": "excluded_downtime"})
    task_deduplicated      = bq_store.count_tasks({"match_status": "deduplicated"})

    # ── TN — patient snapshots within study window, minus confirmed events ─────
    # GCS-backed snapshot count (each hourly snapshot per patient = 1 patient-hour)
    total_patient_hours = db["snapshots"].count_documents(snap_time_q)
    tn = max(0, total_patient_hours - tp - fp - fn)
    total = tp + fp + fn + tn

    # ── Derived metrics ───────────────────────────────────────────────────────
    sensitivity  = round(tp / (tp + fn), 4) if (tp + fn) > 0 else None
    specificity  = round(tn / (tn + fp), 4) if (tn + fp) > 0 else None
    ppv          = round(tp / (tp + fp), 4) if (tp + fp) > 0 else None
    npv          = round(tn / (tn + fn), 4) if (tn + fn) > 0 else None
    f1           = (
        round(2 * ppv * sensitivity / (ppv + sensitivity), 4)
        if ppv and sensitivity and (ppv + sensitivity) > 0
        else None
    )

    sens_ci  = _wilson_ci(tp, tp + fn)
    spec_ci  = _wilson_ci(tn, tn + fp)
    ppv_ci   = _wilson_ci(tp, tp + fp)

    # ── Lead time (minutes) for matched TPs ───────────────────────────────────
    matched_alerts   = bq_store.find_matched_alerts_with_sbar()
    lead_times_min: list[float] = []
    for alert in matched_alerts:
        sbar_rows  = bq_store.find_sbars({"sbar_id": alert["matched_sbar_id"]})
        if not sbar_rows:
            continue
        sbar       = sbar_rows[0]
        alerted_at = alert.get("alerted_at")
        create_dt  = sbar.get("create_date_time")
        if not alerted_at or not create_dt:
            continue
        if isinstance(alerted_at, datetime) and alerted_at.tzinfo is None:
            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
        if isinstance(create_dt, datetime) and create_dt.tzinfo is None:
            create_dt = create_dt.replace(tzinfo=timezone.utc)
        lead_min = (create_dt - alerted_at).total_seconds() / 60
        lead_times_min.append(round(lead_min, 1))

    lead_time_median = round(statistics.median(lead_times_min), 1) if lead_times_min else None
    lead_time_iqr    = None
    if len(lead_times_min) >= 4:
        sorted_lt = sorted(lead_times_min)
        q1 = sorted_lt[len(sorted_lt) // 4]
        q3 = sorted_lt[3 * len(sorted_lt) // 4]
        lead_time_iqr = round(q3 - q1, 1)

    # ── Suppression analysis ──────────────────────────────────────────────────
    suppressed_docs  = bq_store.find_suppressed({})
    suppressed_total = len(suppressed_docs)
    suppressed_with_sbar = 0
    for sup in suppressed_docs:
        sup_at = sup.get("suppressed_at")
        if not sup_at:
            continue
        if isinstance(sup_at, datetime) and sup_at.tzinfo is None:
            sup_at = sup_at.replace(tzinfo=timezone.utc)
        window_start = sup_at - timedelta(hours=_MATCH_WINDOW_BEFORE)
        window_end   = sup_at + timedelta(hours=_MATCH_WINDOW_AFTER)
        has_sbar = bq_store.count_sbars({
            "CPMRN":            sup["CPMRN"],
            "encounter":        sup["encounter"],
            "create_date_time": {"$gte": window_start, "$lte": window_end},
        }) > 0
        if has_sbar:
            suppressed_with_sbar += 1

    # ── Explainability ratings ─────────────────────────────────────────────────
    adj_docs            = bq_store.find_adjudications()
    adj_total           = len(adj_docs)
    expl_clear          = sum(1 for a in adj_docs if a.get("explainability_rating") == 1)
    expl_partial        = sum(1 for a in adj_docs if a.get("explainability_rating") == 2)
    expl_unclear        = sum(1 for a in adj_docs if a.get("explainability_rating") == 3)
    adj_appropriate     = sum(1 for a in adj_docs if a.get("verdict") == "Appropriate")
    adj_inappropriate   = sum(1 for a in adj_docs if a.get("verdict") == "Inappropriate")

    # ── Pending adjudication queue depth ─────────────────────────────────────
    pending_adj  = bq_store.count_alerts({"match_status": "fp_candidate"})
    alerts_total = bq_store.count_alerts({})
    sbars_total  = bq_store.count_sbars({})
    tasks_total  = bq_store.count_tasks({})

    snapshot = {
        "computed_at":              now,
        # 2x2
        "tp":                       tp,
        "fp":                       fp,
        "fn":                       fn,
        "fn_sbar":                  fn_sbar,
        "fn_task":                  fn_task,
        "tn":                       tn,
        "total_patient_hours":      total_patient_hours,
        # Metrics
        "sensitivity":              sensitivity,
        "specificity":              specificity,
        "ppv":                      ppv,
        "npv":                      npv,
        "f1":                       f1,
        "sensitivity_ci":           list(sens_ci),
        "specificity_ci":           list(spec_ci),
        "ppv_ci":                   list(ppv_ci),
        # Lead time
        "lead_time_median_minutes": lead_time_median,
        "lead_time_iqr_minutes":    lead_time_iqr,
        "lead_time_n":              len(lead_times_min),
        # Suppression
        "suppressed_total":         suppressed_total,
        "suppressed_with_sbar":     suppressed_with_sbar,
        # Adjudication
        "pending_adjudication":     pending_adj,
        "adj_total":                adj_total,
        "adj_appropriate":          adj_appropriate,
        "adj_inappropriate":        adj_inappropriate,
        "expl_clear":               expl_clear,
        "expl_partial":             expl_partial,
        "expl_unclear":             expl_unclear,
        # Totals
        "alerts_total":             alerts_total,
        "sbars_total":              sbars_total,
        "tasks_total":              tasks_total,
        "excluded_downtime":        excluded_downtime,
        "task_excluded_downtime":   task_excluded_downtime,
        "task_deduplicated":        task_deduplicated,
    }

    bq_store.insert_metrics_snapshot(snapshot)
    logger.info(
        "study_metrics: TP=%d FP=%d FN=%d (sbar=%d task=%d) TN=%d  sens=%s spec=%s f1=%s  "
        "pending_adj=%d  excluded_downtime=%d  task_dedup=%d",
        tp, fp, fn, fn_sbar, fn_task, tn, sensitivity, specificity, f1,
        pending_adj, excluded_downtime, task_deduplicated,
    )

    return {"metrics": "ok", "tp": tp, "fp": fp, "fn": fn, "tn": tn, "f1": f1}
