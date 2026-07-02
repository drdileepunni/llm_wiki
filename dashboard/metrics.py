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
      COALESCE(delta_reports, 0) AS delta_reports,
      COALESCE(trigger_reason, '') AS trigger_reason,
      problems_scoped,
      gate_trace
    FROM {fqn("pipeline_patient_runs")}
    WHERE run_started_at = TIMESTAMP('{run_started_at}')
    ORDER BY CPMRN
    """
    rows = query(sql)
    import json as _json
    result = []
    for r in rows:
        pd_raw = r.get("problem_details")
        problems = []
        if pd_raw:
            try:
                problems = _json.loads(pd_raw) if isinstance(pd_raw, str) else pd_raw
            except Exception:
                problems = []
        ps_raw = r.get("problems_scoped")
        problems_scoped = None
        if ps_raw:
            try:
                problems_scoped = _json.loads(ps_raw) if isinstance(ps_raw, str) else ps_raw
            except Exception:
                problems_scoped = None
        gt_raw = r.get("gate_trace")
        gate_trace = None
        if gt_raw:
            try:
                gate_trace = _json.loads(gt_raw) if isinstance(gt_raw, str) else gt_raw
            except Exception:
                gate_trace = None
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
            "trigger_reason":   r.get("trigger_reason", ""),
            "problems_scoped":  problems_scoped,  # null = full run; array = scoped names
            "gate_trace":       gate_trace,
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


# ── study metrics (protocol persistence + resolution outcomes) ─────────────────
#
# NOTE: alert_source, protocol_ids (on study_alerts), and problem_resolution_events
# only started being written from the session that added them — any date range
# spanning earlier data will undercount these fields, not because nothing happened
# but because it wasn't recorded yet.

_PATIENT_KEY = "CONCAT(CPMRN, '#', CAST(encounter AS STRING))"


def get_study_summary(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    """Top-line counts for the research study: monitoring volume, silent resolutions, alert mix."""
    runs_filter  = _date_clause("run_started_at", from_date, to_date)
    res_filter   = _date_clause("resolved_at", from_date, to_date)
    alert_filter = _date_clause("alerted_at", from_date, to_date)

    sql = f"""
    WITH patients AS (
        SELECT DISTINCT {_PATIENT_KEY} AS patient_key
        FROM {fqn("pipeline_patient_runs")}
        WHERE 1=1 {runs_filter}
    ),
    resolutions AS (
        SELECT was_ever_alerted
        FROM {fqn("problem_resolution_events")}
        WHERE 1=1 {res_filter}
    ),
    alerts AS (
        SELECT alert_id, COALESCE(alert_source, 'llm_reasoned') AS alert_source
        FROM {fqn("study_alerts")}
        WHERE 1=1 {alert_filter}
    )
    SELECT
        (SELECT COUNT(*) FROM patients)                                          AS patients_monitored,
        (SELECT COUNT(*) FROM resolutions)                                       AS resolutions_total,
        (SELECT COUNTIF(NOT was_ever_alerted) FROM resolutions)                  AS resolved_without_alert,
        (SELECT COUNT(DISTINCT alert_id) FROM alerts WHERE alert_source = 'llm_reasoned')     AS llm_alerts,
        (SELECT COUNT(DISTINCT alert_id) FROM alerts WHERE alert_source = 'care_gap_shortcut') AS care_gap_alerts
    """
    rows = query(sql)
    row = rows[0] if rows else {}
    resolutions_total     = int(row.get("resolutions_total") or 0)
    resolved_without_alert = int(row.get("resolved_without_alert") or 0)
    return {
        "patients_monitored":      int(row.get("patients_monitored") or 0),
        "resolutions_total":       resolutions_total,
        "resolved_without_alert":  resolved_without_alert,
        "resolved_without_alert_pct": round(resolved_without_alert / resolutions_total * 100, 1) if resolutions_total else 0,
        "llm_alerts":              int(row.get("llm_alerts") or 0),
        "care_gap_alerts":         int(row.get("care_gap_alerts") or 0),
    }


def get_alerts_by_protocol(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Alert counts grouped by matched protocol_id. 'none' = no seeded protocol matched (pure model reasoning)."""
    date_filter = _date_clause("alerted_at", from_date, to_date)
    sql = f"""
    SELECT
        IF(pid = '', 'none', pid) AS protocol_id,
        COALESCE(alert_source, 'llm_reasoned') AS alert_source,
        COUNT(DISTINCT alert_id) AS alert_count
    FROM {fqn("study_alerts")},
        UNNEST(IF(protocol_ids IS NULL OR protocol_ids = '', [''], SPLIT(protocol_ids, ','))) AS pid
    WHERE 1=1 {date_filter}
    GROUP BY 1, 2
    ORDER BY alert_count DESC
    """
    rows = query(sql)
    return [
        {
            "protocol_id":  r.get("protocol_id", "none"),
            "alert_source": r.get("alert_source", "llm_reasoned"),
            "alert_count":  int(r.get("alert_count") or 0),
        }
        for r in rows
    ]


def get_monitoring_by_protocol(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """Distinct monitored problems grouped by matched protocol_id."""
    date_filter = _date_clause("run_started_at", from_date, to_date)
    sql = f"""
    SELECT
        IF(pid = '', 'none', pid) AS protocol_id,
        COUNT(DISTINCT CONCAT(CPMRN, '#', CAST(encounter AS STRING), '#', problem_name)) AS problem_count
    FROM {fqn("patient_next_checks")},
        UNNEST(IF(protocol_ids IS NULL OR protocol_ids = '', [''], SPLIT(protocol_ids, ','))) AS pid
    WHERE 1=1 {date_filter}
    GROUP BY 1
    ORDER BY problem_count DESC
    """
    rows = query(sql)
    return [
        {"protocol_id": r.get("protocol_id", "none"), "problem_count": int(r.get("problem_count") or 0)}
        for r in rows
    ]


def get_cost_by_tier(from_date: str | None = None, to_date: str | None = None) -> dict[str, Any]:
    """
    Weighted-average cost for expensive vs. cheap pipeline runs across the date range,
    derived from pipeline_run_costs.patient_tiers (JSON: expensive_count, cheap_count,
    avg_cost_expensive_usd, avg_cost_cheap_usd per run).
    """
    date_filter = _date_clause("run_started_at", from_date, to_date)
    rows = query(f"""
    SELECT patient_tiers
    FROM {fqn("pipeline_run_costs")}
    WHERE 1=1 {date_filter}
    """)
    exp_cost = exp_count = cheap_cost = cheap_count = 0.0
    for r in rows:
        tiers = parse_json_col(r.get("patient_tiers"))
        if not tiers:
            continue
        ec = float(tiers.get("expensive_count") or 0)
        cc = float(tiers.get("cheap_count") or 0)
        exp_cost   += float(tiers.get("avg_cost_expensive_usd") or 0) * ec
        exp_count  += ec
        cheap_cost += float(tiers.get("avg_cost_cheap_usd") or 0) * cc
        cheap_count += cc
    return {
        "expensive_run_count":    int(exp_count),
        "avg_cost_per_expensive_run": round(exp_cost / exp_count, 6) if exp_count else 0,
        "cheap_run_count":        int(cheap_count),
        "avg_cost_per_cheap_run": round(cheap_cost / cheap_count, 6) if cheap_count else 0,
    }


def get_daily_cost_per_patient(from_date: str | None = None, to_date: str | None = None) -> list[dict]:
    """
    Estimated per-patient-per-day cost: total run cost that day / distinct patients
    monitored that day. This is an EVEN-SPLIT ESTIMATE, not a true per-patient
    attribution — the underlying LLM billing is tracked per pipeline run (batched
    across all patients touched that run), not itemized per patient.
    """
    cost_filter    = _date_clause("run_started_at", from_date, to_date)
    patient_filter = _date_clause("run_started_at", from_date, to_date)

    cost_rows = query(f"""
    SELECT DATE(run_started_at) AS day, totals
    FROM {fqn("pipeline_run_costs")}
    WHERE 1=1 {cost_filter}
    """)
    daily_cost: dict[str, float] = {}
    for r in cost_rows:
        day = str(r.get("day"))
        totals = parse_json_col(r.get("totals"))
        daily_cost[day] = daily_cost.get(day, 0.0) + float(totals.get("cost_usd") or 0)

    patient_rows = query(f"""
    SELECT DATE(run_started_at) AS day, COUNT(DISTINCT {_PATIENT_KEY}) AS patients
    FROM {fqn("pipeline_patient_runs")}
    WHERE 1=1 {patient_filter}
    GROUP BY 1
    """)
    daily_patients = {str(r.get("day")): int(r.get("patients") or 0) for r in patient_rows}

    days = sorted(set(daily_cost) | set(daily_patients))
    result = []
    for day in days:
        cost = daily_cost.get(day, 0.0)
        patients = daily_patients.get(day, 0)
        result.append({
            "day":                 day,
            "cost_usd":            round(cost, 4),
            "patients_monitored":  patients,
            "estimated_cost_per_patient": round(cost / patients, 4) if patients else 0,
        })
    return result


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
