"""
BigQuery metric queries for the CDS pipeline dashboard.
All functions accept optional date-range strings (ISO, e.g. "2026-06-01").
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .bq import query, fqn, parse_json_col


# ── helpers ────────────────────────────────────────────────────────────────────

def _date_clause(alias: str, from_date: str | None, to_date: str | None) -> str:
    parts = []
    if from_date:
        parts.append(f"{alias} >= TIMESTAMP('{from_date}')")
    if to_date:
        parts.append(f"{alias} <= TIMESTAMP('{to_date} 23:59:59')")
    return (" AND " + " AND ".join(parts)) if parts else ""


# ── summary ────────────────────────────────────────────────────────────────────

def get_summary(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    date_filter = _date_clause("alerted_at", from_date, to_date)
    sql = f"""
    WITH alerts AS (
        SELECT alert_id, alerted_at
        FROM {fqn("study_alerts")}
        WHERE 1=1 {date_filter}
    ),
    feedback AS (
        SELECT f.alert_id, f.rating, f.user_email
        FROM {fqn("study_alert_feedback")} f
        JOIN alerts a USING (alert_id)
    )
    SELECT
        (SELECT COUNT(DISTINCT alert_id) FROM alerts)              AS alerts_sent,
        COUNT(DISTINCT f.alert_id)                                 AS alerts_rated,
        COUNT(*)                                                   AS total_ratings,
        COUNT(DISTINCT f.user_email)                               AS rater_count,
        ROUND(AVG(f.rating), 2)                                    AS mean_rating,
        COUNTIF(f.rating = 1)                                      AS r1,
        COUNTIF(f.rating = 2)                                      AS r2,
        COUNTIF(f.rating = 3)                                      AS r3,
        COUNTIF(f.rating = 4)                                      AS r4,
        COUNTIF(f.rating = 5)                                      AS r5
    FROM feedback f
    """
    rows = query(sql)
    row = rows[0] if rows else {}
    alerts_sent  = row.get("alerts_sent") or 0
    alerts_rated = row.get("alerts_rated") or 0
    return {
        "alerts_sent":    int(alerts_sent),
        "alerts_rated":   int(alerts_rated),
        "response_rate":  round(alerts_rated / alerts_sent * 100, 1) if alerts_sent else 0,
        "total_ratings":  int(row.get("total_ratings") or 0),
        "rater_count":    int(row.get("rater_count") or 0),
        "mean_rating":    float(row.get("mean_rating") or 0),
        "distribution":   {
            "1": int(row.get("r1") or 0),
            "2": int(row.get("r2") or 0),
            "3": int(row.get("r3") or 0),
            "4": int(row.get("r4") or 0),
            "5": int(row.get("r5") or 0),
        },
    }


# ── timeseries ─────────────────────────────────────────────────────────────────

def get_timeseries(
    bucket: str = "day",
    from_date: str | None = None,
    to_date: str | None = None,
) -> list[dict]:
    trunc = "DAY" if bucket == "day" else "WEEK"
    date_filter = _date_clause("alerted_at", from_date, to_date)
    sql = f"""
    WITH alerts AS (
        SELECT alert_id, DATE_TRUNC(alerted_at, {trunc}) AS period
        FROM {fqn("study_alerts")}
        WHERE 1=1 {date_filter}
    ),
    rated AS (
        SELECT f.alert_id, DATE_TRUNC(f.created_at, {trunc}) AS period, f.rating
        FROM {fqn("study_alert_feedback")} f
        JOIN alerts a USING (alert_id)
    )
    SELECT
        a.period,
        COUNT(DISTINCT a.alert_id)  AS alerts_sent,
        COUNT(DISTINCT r.alert_id)  AS alerts_rated,
        ROUND(AVG(r.rating), 2)     AS mean_rating
    FROM alerts a
    LEFT JOIN rated r USING (alert_id)
    GROUP BY 1
    ORDER BY 1
    """
    rows = query(sql)
    result = []
    for r in rows:
        period = r.get("period")
        if hasattr(period, "isoformat"):
            period = period.isoformat()
        else:
            period = str(period)
        sent   = int(r.get("alerts_sent") or 0)
        rated  = int(r.get("alerts_rated") or 0)
        result.append({
            "period":        period,
            "alerts_sent":   sent,
            "alerts_rated":  rated,
            "response_rate": round(rated / sent * 100, 1) if sent else 0,
            "mean_rating":   float(r.get("mean_rating") or 0),
        })
    return result


# ── by problem ────────────────────────────────────────────────────────────────

def get_by_problem(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    date_filter = _date_clause("a.alerted_at", from_date, to_date)
    sql = f"""
    SELECT
        a.problem_name,
        COUNT(DISTINCT a.alert_id)  AS alerts_sent,
        COUNT(DISTINCT f.alert_id)  AS alerts_rated,
        ROUND(AVG(f.rating), 2)     AS mean_rating,
        COUNTIF(f.rating >= 4)      AS good_ratings,
        COUNTIF(f.rating <= 2)      AS poor_ratings
    FROM {fqn("study_alerts")} a
    LEFT JOIN {fqn("study_alert_feedback")} f USING (alert_id)
    WHERE 1=1 {date_filter}
    GROUP BY 1
    ORDER BY alerts_sent DESC
    """
    rows = query(sql)
    return [
        {
            "problem_name":  r.get("problem_name", "unknown"),
            "alerts_sent":   int(r.get("alerts_sent") or 0),
            "alerts_rated":  int(r.get("alerts_rated") or 0),
            "mean_rating":   float(r.get("mean_rating") or 0) if r.get("mean_rating") else None,
            "good_ratings":  int(r.get("good_ratings") or 0),
            "poor_ratings":  int(r.get("poor_ratings") or 0),
        }
        for r in rows
    ]


# ── by rater ──────────────────────────────────────────────────────────────────

def get_by_rater(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    date_filter = _date_clause("a.alerted_at", from_date, to_date)
    sql = f"""
    SELECT
        f.user_display,
        f.user_email,
        COUNT(*)               AS ratings_count,
        ROUND(AVG(f.rating), 2) AS mean_rating,
        COUNTIF(f.rating = 5)   AS r5,
        COUNTIF(f.rating = 4)   AS r4,
        COUNTIF(f.rating = 3)   AS r3,
        COUNTIF(f.rating = 2)   AS r2,
        COUNTIF(f.rating = 1)   AS r1
    FROM {fqn("study_alert_feedback")} f
    JOIN {fqn("study_alerts")} a USING (alert_id)
    WHERE 1=1 {date_filter}
    GROUP BY 1, 2
    ORDER BY ratings_count DESC
    """
    rows = query(sql)
    return [
        {
            "user_display":   r.get("user_display", ""),
            "user_email":     r.get("user_email", ""),
            "ratings_count":  int(r.get("ratings_count") or 0),
            "mean_rating":    float(r.get("mean_rating") or 0),
            "distribution":   {str(i): int(r.get(f"r{i}") or 0) for i in range(1, 6)},
        }
        for r in rows
    ]


# ── cost ──────────────────────────────────────────────────────────────────────

def get_cost(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    date_filter = _date_clause("run_started_at", from_date, to_date)
    run_rows = query(f"""
    SELECT run_started_at, patient_count, totals, by_step, model
    FROM {fqn("pipeline_run_costs")}
    WHERE 1=1 {date_filter}
    ORDER BY run_started_at ASC
    """)
    if not run_rows:
        return {
            "run_count": 0, "total_patients": 0,
            "total_cost_usd": 0, "avg_cost_per_run": 0, "avg_cost_per_patient": 0,
            "timeseries": [], "by_step": {},
        }

    run_count      = len(run_rows)
    total_patients = sum(int(r.get("patient_count") or 0) for r in run_rows)

    timeseries = []
    step_totals: dict[str, float] = {}
    total_cost = 0.0

    for r in run_rows:
        totals  = parse_json_col(r.get("totals"))
        by_step = parse_json_col(r.get("by_step"))
        cost    = float(totals.get("cost_usd") or 0)
        total_cost += cost
        ts = r.get("run_started_at")
        timeseries.append({
            "run_started_at": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "patient_count":  int(r.get("patient_count") or 0),
            "cost_usd":       round(cost, 6),
            "model":          r.get("model", ""),
        })
        for step, step_data in by_step.items():
            step_cost = float(step_data.get("cost_usd") or 0)
            step_totals[step] = step_totals.get(step, 0) + step_cost

    return {
        "run_count":          run_count,
        "total_patients":     total_patients,
        "total_cost_usd":     round(total_cost, 4),
        "avg_cost_per_run":   round(total_cost / run_count, 4) if run_count else 0,
        "avg_cost_per_patient": round(total_cost / total_patients, 4) if total_patients else 0,
        "timeseries":         timeseries,
        "by_step":            {k: round(v, 4) for k, v in sorted(step_totals.items(), key=lambda x: -x[1])},
    }


# ── alerts per run ────────────────────────────────────────────────────────────

def get_alerts_per_run(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    date_filter = _date_clause("r.run_started_at", from_date, to_date)
    sql = f"""
    WITH runs AS (
      SELECT
        run_started_at,
        LEAD(run_started_at) OVER (ORDER BY run_started_at) AS next_run
      FROM {fqn("pipeline_run_costs")}
      WHERE 1=1 {date_filter}
    )
    SELECT
      r.run_started_at,
      COUNT(DISTINCT a.alert_id) AS alerts_sent
    FROM runs r
    LEFT JOIN {fqn("study_alerts")} a
      ON a.alerted_at >= r.run_started_at
      AND (r.next_run IS NULL OR a.alerted_at < r.next_run)
    GROUP BY 1
    ORDER BY 1
    """
    rows = query(sql)
    timeseries = []
    total_alerts = 0
    for r in rows:
        ts = r.get("run_started_at")
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        alerts = int(r.get("alerts_sent") or 0)
        total_alerts += alerts
        timeseries.append({"run_started_at": ts_str, "alerts_sent": alerts})
    run_count = len(timeseries)
    return {
        "timeseries":       timeseries,
        "avg_alerts_per_run": round(total_alerts / run_count, 1) if run_count else 0,
        "run_count":        run_count,
    }


# ── per-run details ───────────────────────────────────────────────────────────

def get_runs(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Return one row per pipeline run with patient tier breakdown, cost, and alerts."""
    date_filter = _date_clause("r.run_started_at", from_date, to_date)
    sql = f"""
    WITH runs AS (
      SELECT
        run_started_at,
        patient_count,
        trace_count,
        totals,
        patient_tiers,
        LEAD(run_started_at) OVER (ORDER BY run_started_at) AS next_run
      FROM {fqn("pipeline_run_costs")}
      WHERE 1=1 {date_filter}
    ),
    run_alerts AS (
      SELECT
        r.run_started_at,
        COUNT(DISTINCT a.alert_id) AS alerts_sent
      FROM runs r
      LEFT JOIN {fqn("study_alerts")} a
        ON a.alerted_at >= r.run_started_at
        AND (r.next_run IS NULL OR a.alerted_at < r.next_run)
      GROUP BY 1
    ),
    report_stats AS (
      SELECT
        r.run_started_at,
        COUNTIF(ri.n_reports_interpreted > 0)   AS report_charts,
        ROUND(SUM(ri.cost_usd), 6)              AS report_cost_usd
      FROM runs r
      LEFT JOIN {fqn("report_interpret_runs")} ri
        ON ri.cycle_started_at >= r.run_started_at
        AND (r.next_run IS NULL OR ri.cycle_started_at < r.next_run)
      GROUP BY 1
    )
    SELECT
      r.run_started_at,
      r.patient_count,
      r.trace_count,
      r.totals,
      r.patient_tiers,
      COALESCE(ra.alerts_sent, 0)        AS alerts_sent,
      COALESCE(rs.report_charts, 0)      AS report_charts,
      COALESCE(rs.report_cost_usd, 0.0)  AS report_cost_usd
    FROM runs r
    LEFT JOIN run_alerts ra    USING (run_started_at)
    LEFT JOIN report_stats rs  USING (run_started_at)
    ORDER BY r.run_started_at DESC
    """
    rows = query(sql)
    result = []
    for r in rows:
        totals = parse_json_col(r.get("totals"))
        tiers  = parse_json_col(r.get("patient_tiers"))
        ts = r.get("run_started_at")
        result.append({
            "run_started_at":         ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
            "patient_count":          int(r.get("patient_count") or 0),
            "trace_count":            int(r.get("trace_count") or 0),
            "alerts_sent":            int(r.get("alerts_sent") or 0),
            "cost_usd":               round(float(totals.get("cost_usd") or 0), 4),
            "expensive_count":        int(tiers.get("expensive_count") or 0) if tiers else None,
            "cheap_count":            int(tiers.get("cheap_count") or 0) if tiers else None,
            "skipped_count":          int(tiers.get("skipped_count") or 0) if tiers else None,
            "total_scheduled":        int(tiers.get("total_scheduled") or 0) if tiers else None,
            "avg_cost_expensive_usd": round(float(tiers.get("avg_cost_expensive_usd") or 0), 6) if tiers else None,
            "avg_cost_cheap_usd":     round(float(tiers.get("avg_cost_cheap_usd") or 0), 6) if tiers else None,
            "report_charts":          int(r.get("report_charts") or 0),
            "report_cost_usd":        round(float(r.get("report_cost_usd") or 0), 4),
        })
    return result


# ── run patient audit ─────────────────────────────────────────────────────────

def get_run_patient_audit(run_started_at: str) -> list[dict]:
    """Return per-patient audit rows for a given run (identified by run_started_at ISO string)."""
    sql = f"""
    SELECT
      CPMRN,
      encounter,
      pipeline_outcome,
      pass1_tag,
      pass1_needs_full,
      pass2_outcome,
      problem_details,
      COALESCE(delta_vitals, 0)  AS delta_vitals,
      COALESCE(delta_labs, 0)    AS delta_labs,
      COALESCE(delta_notes, 0)   AS delta_notes,
      COALESCE(delta_reports, 0) AS delta_reports
    FROM {fqn("pipeline_patient_runs")}
    WHERE run_started_at = TIMESTAMP('{run_started_at}')
    ORDER BY CPMRN
    """
    rows = query(sql)
    result = []
    for r in rows:
        pd_raw = r.get("problem_details")
        problems = []
        if pd_raw:
            try:
                import json as _json
                problems = _json.loads(pd_raw) if isinstance(pd_raw, str) else pd_raw
            except Exception:
                problems = []
        result.append({
            "CPMRN":            r.get("CPMRN", ""),
            "encounter":        int(r.get("encounter") or 0),
            "pipeline_outcome": r.get("pipeline_outcome", ""),
            "pass1_tag":        r.get("pass1_tag", ""),
            "pass1_needs_full": bool(r.get("pass1_needs_full", False)),
            "pass2_outcome":    r.get("pass2_outcome", ""),
            "problems":         problems,
            "delta_vitals":     int(r.get("delta_vitals") or 0),
            "delta_labs":       int(r.get("delta_labs") or 0),
            "delta_notes":      int(r.get("delta_notes") or 0),
            "delta_reports":    int(r.get("delta_reports") or 0),
        })
    return result


# ── feedback comments ─────────────────────────────────────────────────────────

def get_comments(limit: int = 50, from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    date_filter = _date_clause("f.created_at", from_date, to_date)
    sql = f"""
    SELECT
        f.alert_id,
        f.problem_name,
        f.rating,
        f.feedback_text,
        f.user_display,
        f.created_at
    FROM {fqn("study_alert_feedback")} f
    WHERE f.feedback_text IS NOT NULL AND TRIM(f.feedback_text) != ''
    {date_filter}
    ORDER BY f.created_at DESC
    LIMIT {int(limit)}
    """
    rows = query(sql)
    return [
        {
            "alert_id":     r.get("alert_id", ""),
            "problem_name": r.get("problem_name", ""),
            "rating":       int(r.get("rating") or 0),
            "comment":      r.get("feedback_text", ""),
            "user":         r.get("user_display", ""),
            "created_at":   (r["created_at"].isoformat()
                             if hasattr(r.get("created_at"), "isoformat")
                             else str(r.get("created_at", ""))),
        }
        for r in rows
    ]


# ── raw ratings for agreement ──────────────────────────────────────────────────

def get_ratings_per_alert(from_date: str | None = None, to_date: str | None = None) -> dict[str, list[int]]:
    """Return {alert_id: [rating1, rating2, ...]} for alerts with ≥1 rating."""
    date_filter = _date_clause("a.alerted_at", from_date, to_date)
    sql = f"""
    SELECT f.alert_id, f.user_email, f.rating
    FROM {fqn("study_alert_feedback")} f
    JOIN {fqn("study_alerts")} a USING (alert_id)
    WHERE 1=1 {date_filter}
    ORDER BY f.alert_id, f.created_at
    """
    rows = query(sql)
    result: dict[str, list[int]] = {}
    for r in rows:
        aid = r["alert_id"]
        result.setdefault(aid, []).append(int(r.get("rating") or 0))
    return result
