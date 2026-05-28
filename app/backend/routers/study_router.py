"""
Study API — endpoints for the adjudication UI and metrics dashboard.

GET  /api/study/queue                  — FP candidate alerts pending review
GET  /api/study/alert/{alert_id}       — full alert detail
POST /api/study/adjudicate/{alert_id}  — submit verdict + explainability rating
GET  /api/study/metrics                — latest metrics snapshot
GET  /api/study/metrics/history        — time series (last N snapshots)
"""
from __future__ import annotations

import logging
import sys
import uuid as _uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/study", tags=["study"])
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
for _p in [str(_ROOT / "app"), str(_ROOT)]:
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _col(name: str):
    from backend.services.emr.db import get_db
    return get_db()[name]


def _ser(doc: dict) -> dict:
    """Serialize a MongoDB document — convert ObjectId/datetime to strings."""
    out = {}
    for k, v in doc.items():
        if isinstance(v, ObjectId):
            out[k] = str(v)
        elif isinstance(v, datetime):
            # Always emit UTC-marked strings so JavaScript parses them as UTC
            # regardless of the browser's local timezone.
            if v.tzinfo is None:
                v = v.replace(tzinfo=timezone.utc)
            out[k] = v.isoformat()
        elif isinstance(v, list):
            out[k] = [_ser(i) if isinstance(i, dict) else (str(i) if isinstance(i, ObjectId) else i) for i in v]
        elif isinstance(v, dict):
            out[k] = _ser(v)
        else:
            out[k] = v
    return out


# ── Models ─────────────────────────────────────────────────────────────────────

class AdjudicationRequest(BaseModel):
    verdict:              str        # "Appropriate" | "Inappropriate"
    explainability_rating: int | None = None  # 1=clear, 2=partial, 3=unclear (optional)
    reviewer_notes:       str = ""


class FNAdjudicationRequest(BaseModel):
    verdict:        str        # "true_miss" | "excused" | "cooldown_miss"
    excusal_reason: str = ""   # required when verdict == "excused"
    reviewer_notes: str = ""


class StudyCreate(BaseModel):
    name:     str
    start_dt: str           # ISO 8601 datetime string
    end_dt:   Optional[str] = None  # ISO 8601 or null (ongoing)
    status:   str = "active"


class StudyUpdate(BaseModel):
    name:     Optional[str] = None
    start_dt: Optional[str] = None
    end_dt:   Optional[str] = None
    status:   Optional[str] = None


# Default study window when no study_id is supplied (legacy behaviour)
_STUDY_START_FALLBACK = datetime(2026, 5, 26, 11, 0, 0, tzinfo=timezone.utc)


def _parse_iso(s: str) -> datetime:
    """Parse an ISO 8601 string to a UTC-aware datetime."""
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _resolve_time_window(study_id: str) -> tuple[datetime, datetime | None]:
    """Return (start_dt, end_dt) for a study. Raises 404 if not found."""
    study = _col("studies").find_one({"study_id": study_id})
    if not study:
        raise HTTPException(status_code=404, detail=f"Study '{study_id}' not found")
    start_dt = study["start_dt"]
    end_dt   = study.get("end_dt")
    if isinstance(start_dt, str):
        start_dt = _parse_iso(start_dt)
    elif isinstance(start_dt, datetime) and start_dt.tzinfo is None:
        start_dt = start_dt.replace(tzinfo=timezone.utc)
    if isinstance(end_dt, str):
        end_dt = _parse_iso(end_dt)
    elif isinstance(end_dt, datetime) and end_dt.tzinfo is None:
        end_dt = end_dt.replace(tzinfo=timezone.utc)
    return start_dt, end_dt


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/queue")
def get_queue(study_id: Optional[str] = None):
    """Return all fp_candidate alerts pending adjudication, oldest first."""
    time_filter: dict = {}
    if study_id:
        start_dt, end_dt = _resolve_time_window(study_id)
        time_filter = {"alerted_at": {"$gte": start_dt}}
        if end_dt:
            time_filter["alerted_at"]["$lte"] = end_dt
    alerts = list(
        _col("study_alerts")
        .find({"match_status": "fp_candidate", **time_filter})
        .sort("alerted_at", 1)
        .limit(200)
    )
    return {"queue": [_ser(a) for a in alerts], "count": len(alerts)}


@router.get("/alert/{alert_id}")
def get_alert(alert_id: str):
    """Return full alert detail for the review panel."""
    try:
        oid = ObjectId(alert_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid alert_id")

    alert = _col("study_alerts").find_one({"_id": oid})
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    from datetime import timedelta

    result = _ser(alert)

    # Attach chart URL
    result["chart_url"] = (
        f"https://cloudphysicianworld.com/patient/{alert['CPMRN']}/{alert.get('encounter', 1)}"
    )

    # Attach any matched SBAR info (for context)
    if alert.get("matched_sbar_id"):
        sbar = _col("study_sbar_import").find_one({"sbar_id": alert["matched_sbar_id"]})
        if sbar:
            result["matched_sbar"] = _ser(sbar)

    # Normalise alerted_at to UTC-aware datetime
    alerted_at = alert.get("alerted_at")
    if isinstance(alerted_at, datetime) and alerted_at.tzinfo is None:
        alerted_at = alerted_at.replace(tzinfo=timezone.utc)

    # How many hours until the match window closes (if still pending)
    window_open_hours = None
    if alert.get("match_status") in ("pending", "fp_candidate"):
        now = datetime.now(timezone.utc)
        if alerted_at:
            elapsed_h = (now - alerted_at).total_seconds() / 3600
            window_open_hours = round(max(0, 8 - elapsed_h), 1)
    result["window_open_hours"] = window_open_hours

    # Vitals, labs, and latest note in the event window (−8h to +2h)
    if alerted_at:
        w_start = alerted_at - timedelta(hours=8)
        w_end   = alerted_at + timedelta(hours=2)
        result["window_start"] = w_start.isoformat()
        result["window_end"]   = w_end.isoformat()
        result["vitals_in_window"], result["labs_in_window"] = _extract_window_data(
            alert["CPMRN"], alert.get("encounter", 1), w_start, w_end
        )
        # All doctor notes in the window (Progress + Event types), newest first
        result["notes_in_window"] = _fetch_notes_in_window(
            alert["CPMRN"], alert.get("encounter", 1), w_start, alerted_at
        )
    else:
        result["window_start"]     = None
        result["window_end"]       = None
        result["vitals_in_window"] = []
        result["labs_in_window"]   = []
        result["notes_in_window"]  = []

    return result


@router.post("/adjudicate/{alert_id}")
def adjudicate(alert_id: str, req: AdjudicationRequest):
    """Submit a reviewer verdict for an alert."""
    if req.verdict not in ("Appropriate", "Inappropriate"):
        raise HTTPException(status_code=400, detail="verdict must be 'Appropriate' or 'Inappropriate'")
    if req.explainability_rating is not None and req.explainability_rating not in (1, 2, 3):
        raise HTTPException(status_code=400, detail="explainability_rating must be 1, 2, or 3")

    try:
        oid = ObjectId(alert_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid alert_id")

    alert = _col("study_alerts").find_one({"_id": oid})
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    now = datetime.now(timezone.utc)

    # Upsert adjudication record (one per alert — single reviewer)
    _col("study_adjudications").update_one(
        {"alert_id": str(oid)},
        {"$set": {
            "alert_id":             str(oid),
            "verdict":              req.verdict,
            "explainability_rating": req.explainability_rating,
            "reviewer_notes":       req.reviewer_notes,
            "reviewed_at":          now,
        }},
        upsert=True,
    )

    # Update alert match_status to reflect verdict
    new_status = "tp_confirmed" if req.verdict == "Appropriate" else "fp_confirmed"
    _col("study_alerts").update_one(
        {"_id": oid},
        {"$set": {"match_status": new_status, "adjudicated_at": now}},
    )

    logger.info(
        "study_router: adjudicated alert %s → %s (expl=%s)",
        alert_id, req.verdict, req.explainability_rating,
    )

    # Return next item in queue
    next_alert = _col("study_alerts").find_one(
        {"match_status": "fp_candidate", "_id": {"$ne": oid}},
        sort=[("alerted_at", 1)],
    )
    return {
        "ok": True,
        "alert_id": alert_id,
        "verdict": req.verdict,
        "next_alert_id": str(next_alert["_id"]) if next_alert else None,
    }


@router.post("/dismiss/{alert_id}")
def dismiss_alert(alert_id: str):
    """Exclude an alert from the study queue without adjudicating it."""
    try:
        oid = ObjectId(alert_id)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid alert_id")

    alert = _col("study_alerts").find_one({"_id": oid})
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")

    now = datetime.now(timezone.utc)
    _col("study_alerts").update_one(
        {"_id": oid},
        {"$set": {"match_status": "excluded", "excluded_at": now}},
    )

    next_alert = _col("study_alerts").find_one(
        {"match_status": "fp_candidate", "_id": {"$ne": oid}},
        sort=[("alerted_at", 1)],
    )
    return {
        "ok": True,
        "alert_id": alert_id,
        "next_alert_id": str(next_alert["_id"]) if next_alert else None,
    }


@router.get("/metrics")
def get_metrics(study_id: Optional[str] = None):
    """Return live metrics for a study period.

    When study_id is supplied, all counts are scoped to that study's time window.
    When omitted, the fallback start date is used (legacy behaviour).
    """
    now = datetime.now(timezone.utc)

    # ── Resolve time window ───────────────────────────────────────────────────
    study_doc = None
    if study_id:
        start_dt, end_dt = _resolve_time_window(study_id)
        study_doc = _col("studies").find_one({"study_id": study_id})
    else:
        start_dt = _STUDY_START_FALLBACK
        end_dt   = None

    effective_end = end_dt if end_dt else now

    alert_time_q = {"alerted_at":      {"$gte": start_dt, "$lte": effective_end}}
    sbar_time_q  = {"create_date_time": {"$gte": start_dt, "$lte": effective_end}}
    task_time_q  = {"task_visible_at":  {"$gte": start_dt, "$lte": effective_end}}
    snap_time_q  = {"snapshot_at":      {"$gte": start_dt, "$lte": effective_end}}

    # ── 2×2 counts (fully live) ───────────────────────────────────────────────
    _fn_statuses = ["confirmed_fn", "fn_reviewed_miss"]
    alerts_col   = _col("study_alerts")
    sbar_col     = _col("study_sbar_import")
    task_col     = _col("study_task_import")

    tp      = alerts_col.count_documents({"match_status": "tp_confirmed",                       **alert_time_q})
    fp      = alerts_col.count_documents({"match_status": "fp_confirmed",                       **alert_time_q})
    fn_sbar = sbar_col.count_documents(  {"match_status": {"$in": _fn_statuses},                **sbar_time_q})
    fn_task = task_col.count_documents(  {"match_status": {"$in": _fn_statuses},                **task_time_q})
    fn      = fn_sbar + fn_task
    total_patient_hours = _col("snapshots").count_documents(snap_time_q)
    tn = max(0, total_patient_hours - tp - fp - fn)

    # ── FN breakdown ─────────────────────────────────────────────────────────
    fn_unreviewed   = (sbar_col.count_documents({"match_status": "confirmed_fn",    **sbar_time_q}) +
                       task_col.count_documents({"match_status": "confirmed_fn",    **task_time_q}))
    fn_true_miss    = (sbar_col.count_documents({"match_status": "fn_reviewed_miss",**sbar_time_q}) +
                       task_col.count_documents({"match_status": "fn_reviewed_miss",**task_time_q}))
    fn_cooldown_miss= (sbar_col.count_documents({"match_status": "fn_cooldown_miss",**sbar_time_q}) +
                       task_col.count_documents({"match_status": "fn_cooldown_miss",**task_time_q}))
    fn_excused      = (sbar_col.count_documents({"match_status": "fn_excused",      **sbar_time_q}) +
                       task_col.count_documents({"match_status": "fn_excused",      **task_time_q}))

    # ── Derived metrics ───────────────────────────────────────────────────────
    sensitivity = round(tp / (tp + fn), 4) if (tp + fn) > 0 else None
    specificity = round(tn / (tn + fp), 4) if (tn + fp) > 0 else None
    ppv         = round(tp / (tp + fp), 4) if (tp + fp) > 0 else None
    npv         = round(tn / (tn + fn), 4) if (tn + fn) > 0 else None
    f1          = (
        round(2 * ppv * sensitivity / (ppv + sensitivity), 4)
        if ppv and sensitivity and (ppv + sensitivity) > 0 else None
    )

    # ── CI from last stored snapshot (same for all studies — best we have) ───
    snap = _col("study_metrics_snapshots").find_one({}, sort=[("computed_at", -1)])
    sens_ci  = snap.get("sensitivity_ci") if snap else None
    spec_ci  = snap.get("specificity_ci") if snap else None
    ppv_ci   = snap.get("ppv_ci")         if snap else None
    lead_time_median  = snap.get("lead_time_median_minutes")  if snap else None
    lead_time_iqr     = snap.get("lead_time_iqr_minutes")     if snap else None
    lead_time_n       = snap.get("lead_time_n")               if snap else None
    suppressed_total  = snap.get("suppressed_total")          if snap else 0
    suppressed_w_sbar = snap.get("suppressed_with_sbar")      if snap else 0
    computed_at       = snap.get("computed_at")               if snap else None

    # ── Adjudication counts scoped to study window ────────────────────────────
    # Get alert IDs within the study window to filter adjudications
    study_alert_ids = [
        str(a["_id"]) for a in alerts_col.find(alert_time_q, {"_id": 1})
    ]
    adj_filter = {"alert_id": {"$in": study_alert_ids}} if study_alert_ids else {}
    adj_docs   = list(_col("study_adjudications").find(adj_filter, {"verdict": 1, "explainability_rating": 1}))

    adj_total         = len(adj_docs)
    adj_appropriate   = sum(1 for a in adj_docs if a.get("verdict") == "Appropriate")
    adj_inappropriate = sum(1 for a in adj_docs if a.get("verdict") == "Inappropriate")
    expl_clear        = sum(1 for a in adj_docs if a.get("explainability_rating") == 1)
    expl_partial      = sum(1 for a in adj_docs if a.get("explainability_rating") == 2)
    expl_unclear      = sum(1 for a in adj_docs if a.get("explainability_rating") == 3)
    pending_adj       = alerts_col.count_documents({"match_status": "fp_candidate",     **alert_time_q})
    awaiting_queue    = alerts_col.count_documents({"match_status": "pending",           **alert_time_q})

    # ── Study overview ────────────────────────────────────────────────────────
    study_hours = round((effective_end - start_dt).total_seconds() / 3600, 1)

    result: dict = {
        "computed_at":              computed_at.isoformat() if isinstance(computed_at, datetime) else computed_at,
        # 2×2
        "tp": tp, "fp": fp, "fn": fn, "fn_sbar": fn_sbar, "fn_task": fn_task, "tn": tn,
        "total_patient_hours":      total_patient_hours,
        # FN breakdown
        "fn_unreviewed":            fn_unreviewed,
        "fn_true_miss":             fn_true_miss,
        "fn_cooldown_miss":         fn_cooldown_miss,
        "fn_excused":               fn_excused,
        # Metrics
        "sensitivity": sensitivity, "specificity": specificity,
        "ppv": ppv, "npv": npv, "f1": f1,
        "sensitivity_ci": sens_ci,  "specificity_ci": spec_ci, "ppv_ci": ppv_ci,
        # Lead time (from stored snapshot)
        "lead_time_median_minutes": lead_time_median,
        "lead_time_iqr_minutes":    lead_time_iqr,
        "lead_time_n":              lead_time_n,
        # Suppression (from stored snapshot)
        "suppressed_total":         suppressed_total,
        "suppressed_with_sbar":     suppressed_w_sbar,
        # Adjudication
        "adj_total": adj_total, "adj_appropriate": adj_appropriate,
        "adj_inappropriate": adj_inappropriate,
        "expl_clear": expl_clear, "expl_partial": expl_partial, "expl_unclear": expl_unclear,
        "pending_adjudication":     pending_adj,
        # Study overview
        "study_hours":              study_hours,
        "awaiting_queue":           awaiting_queue,
        # Study identity (for UI display)
        "study_id":   study_doc.get("study_id")   if study_doc else None,
        "study_name": study_doc.get("name")        if study_doc else None,
        "study_start_dt": start_dt.isoformat(),
        "study_end_dt":   end_dt.isoformat() if end_dt else None,
    }

    return {"metrics": result}


@router.get("/metrics/history")
def get_metrics_history(n: int = 48):
    """Return the last N metrics snapshots for charting (newest first)."""
    snaps = list(
        _col("study_metrics_snapshots")
        .find({})
        .sort("computed_at", -1)
        .limit(min(n, 500))
    )
    return {"history": [_ser(s) for s in reversed(snaps)], "count": len(snaps)}


@router.get("/costs")
def get_costs(n: int = 48):
    """Return the last N pipeline run cost docs (newest first)."""
    docs = list(
        _col("pipeline_run_costs")
        .find({})
        .sort("run_started_at", -1)
        .limit(min(n, 500))
    )
    return {"costs": [_ser(d) for d in docs], "count": len(docs)}


# ── FN Review Queue ────────────────────────────────────────────────────────────

_FN_REVIEW_STATUSES = {"confirmed_fn"}  # only unreviewed items go into the queue

def _normalise_fn_record(doc: dict, source: str) -> dict:
    """Return a queue-safe dict from a SBAR or task document."""
    anchor = doc.get("create_date_time") if source == "sbar" else doc.get("task_visible_at")
    return {
        "record_id":      doc.get("sbar_id") if source == "sbar" else doc.get("task_id"),
        "source":         source,
        "CPMRN":          doc.get("CPMRN", ""),
        "encounter":      doc.get("encounter", 1),
        "issues":         doc.get("issues", ""),
        "urgency":        doc.get("urgency") if source == "sbar" else doc.get("priority", ""),
        "event_time":     anchor.isoformat() if isinstance(anchor, datetime) else str(anchor or ""),
        "hospital_name":  doc.get("hospital_name", ""),
        "unit_name":      doc.get("unit_name", ""),
        "confirmed_fn_at": (
            doc.get("confirmed_fn_at").isoformat()
            if isinstance(doc.get("confirmed_fn_at"), datetime)
            else str(doc.get("confirmed_fn_at") or "")
        ),
    }


@router.get("/fn-queue")
def get_fn_queue(study_id: Optional[str] = None):
    """Return all unreviewed confirmed_fn records (SBARs + tasks), oldest first."""
    sbar_filter: dict = {"match_status": "confirmed_fn"}
    task_filter: dict = {"match_status": "confirmed_fn"}
    if study_id:
        start_dt, end_dt = _resolve_time_window(study_id)
        sbar_time = {"create_date_time": {"$gte": start_dt}}
        task_time = {"task_visible_at":  {"$gte": start_dt}}
        if end_dt:
            sbar_time["create_date_time"]["$lte"] = end_dt
            task_time["task_visible_at"]["$lte"]  = end_dt
        sbar_filter.update(sbar_time)
        task_filter.update(task_time)

    sbars = list(_col("study_sbar_import").find(sbar_filter))
    tasks = list(_col("study_task_import").find(task_filter))

    items = (
        [_normalise_fn_record(s, "sbar") for s in sbars] +
        [_normalise_fn_record(t, "task") for t in tasks]
    )
    items.sort(key=lambda x: x["event_time"])
    return {"queue": items[:200], "count": len(items)}


@router.get("/fn-record/{record_id}")
def get_fn_record(record_id: str, source: str = "sbar"):
    """
    Return full detail for an FN record (SBAR or task) enriched with:
    - nearby system alerts within the match window
    - suppressed events in window (cooldown hint)
    - last known patient_problems state
    """
    from datetime import timedelta

    if source == "task":
        doc = _col("study_task_import").find_one({"task_id": record_id})
        if not doc:
            raise HTTPException(status_code=404, detail="Task not found")
        anchor = doc.get("task_visible_at")
    else:
        doc = _col("study_sbar_import").find_one({"sbar_id": record_id})
        if not doc:
            raise HTTPException(status_code=404, detail="SBAR not found")
        anchor = doc.get("create_date_time")

    if isinstance(anchor, datetime) and anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)

    result = _ser(doc)
    result["source"]    = source
    result["record_id"] = record_id
    result["chart_url"] = (
        f"https://cloudphysicianworld.com/patient/{doc['CPMRN']}/{doc.get('encounter', 1)}"
    )

    cpmrn    = doc["CPMRN"]
    encounter = doc.get("encounter", 1)

    if anchor:
        window_start = anchor - timedelta(hours=6)
        window_end   = anchor + timedelta(hours=2)

        # System alerts near the event window (even unmatched / suppressed)
        nearby_alerts = list(_col("study_alerts").find(
            {
                "CPMRN":      cpmrn,
                "encounter":  encounter,
                "alerted_at": {"$gte": window_start, "$lte": window_end},
            },
            {"_id": 1, "problem_name": 1, "alert_reason": 1, "alerted_at": 1, "match_status": 1},
        ))
        result["nearby_alerts"] = [_ser(a) for a in nearby_alerts]

        # Suppressed events in window — if any exist, cooldown was the blocker
        suppressed = list(_col("study_suppressed_events").find(
            {
                "CPMRN":        cpmrn,
                "encounter":    encounter,
                "suppressed_at": {"$gte": window_start, "$lte": window_end},
            },
            {"_id": 0, "problem_name": 1, "suppressed_at": 1, "suppression_reason": 1},
        ))
        result["suppressed_events"] = [_ser(s) for s in suppressed]
        result["cooldown_hint"] = len(suppressed) > 0

        # Last known patient_problems state at time of event
        problems = list(_col("patient_problems").find(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"_id": 0, "problem_name": 1, "clinical_status": 1, "being_addressed": 1, "last_alerted_at": 1},
        ))
        result["patient_problems_snapshot"] = [_ser(p) for p in problems]

        # Vitals and labs from snapshots in the event window
        result["window_start"] = window_start.isoformat()
        result["window_end"]   = window_end.isoformat()
        result["vitals_in_window"], result["labs_in_window"] = _extract_window_data(
            cpmrn, encounter, window_start, window_end
        )
    else:
        result["nearby_alerts"]            = []
        result["suppressed_events"]        = []
        result["cooldown_hint"]            = False
        result["patient_problems_snapshot"] = []
        result["vitals_in_window"]         = []
        result["labs_in_window"]           = []
        result["window_start"]             = None
        result["window_end"]               = None

    return result


def _fetch_notes_in_window(
    cpmrn: str,
    encounter: int,
    window_start: datetime,
    window_end: datetime,
) -> list[dict]:
    """
    Return all clinical notes (Progress + Event) within [window_start, window_end],
    sorted newest-first. If none exist in-window, falls back to the single most
    recent note before window_end (marked out_of_window=True).
    Each note is assembled from its first 2 chunks to avoid cumulative-template
    repetition.
    """
    admission_id = f"{cpmrn}_{encounter}"
    doc = _col("note_indexes").find_one({"admission_id": admission_id}, {"chunks": 1})
    if not doc:
        return []

    chunks = doc.get("chunks") or []

    # doc_id format: "{admission_id}_{hash}:{note_index}"
    # The full doc_id uniquely identifies a note; chunk_index orders chunks within it.
    # We must NOT strip the note_index suffix — all notes share the same hash prefix.
    by_note: dict[str, dict] = {}
    for chunk in chunks:
        nt = chunk.get("note_time")
        if not nt:
            continue
        ts = _parse_ts_utc(str(nt))
        if ts is None or ts > window_end:
            continue
        note_id = chunk.get("doc_id", "")   # full doc_id = unique note identifier
        if note_id not in by_note:
            by_note[note_id] = {"ts": ts, "chunk": chunk, "in_window": ts >= window_start}
        else:
            # Keep latest ts for this note; mark in_window if any chunk qualifies
            if ts > by_note[note_id]["ts"]:
                by_note[note_id]["ts"] = ts
                by_note[note_id]["chunk"] = chunk
            if ts >= window_start:
                by_note[note_id]["in_window"] = True

    if not by_note:
        return []

    in_window_notes = {k: v for k, v in by_note.items() if v["in_window"]}
    out_of_window_fallback = len(in_window_notes) == 0

    # Use in-window notes; if none exist, fall back to the single most recent before window
    if out_of_window_fallback:
        latest_id = max(by_note, key=lambda k: by_note[k]["ts"])
        candidates = {latest_id: by_note[latest_id]}
    else:
        candidates = in_window_notes

    notes = []
    for note_id, info in candidates.items():
        # Assemble chunks that share this exact doc_id, sorted by chunk_index
        note_chunks = sorted(
            [c for c in chunks if c.get("doc_id") == note_id],
            key=lambda c: int(c.get("chunk_index", 0)) if str(c.get("chunk_index", 0)).isdigit() else 0,
        )[:2]
        full_text = " ".join(c.get("text", "") for c in note_chunks).strip()
        notes.append({
            "note_time":     info["ts"].isoformat(),
            "note_type":     info["chunk"].get("note_type", ""),
            "author":        info["chunk"].get("author", ""),
            "text":          full_text[:2000],
            "out_of_window": out_of_window_fallback,
        })

    # Newest first
    notes.sort(key=lambda n: n["note_time"], reverse=True)
    return notes


_VITAL_FIELDS = {
    "HR":    "daysHR",
    "SpO2":  "daysSpO2",
    "RR":    "daysRR",
    "BP":    "daysBP",
    "MAP":   "daysMAP",
    "FiO2":  "daysFiO2",
    "Temp":  "daysTemp",
    "GCS":   "daysGCS",
    "GCS-E": "daysGCSeyes",
    "GCS-V": "daysGCSverbal",
    "GCS-M": "daysGCSmotor",
}


def _parse_ts_utc(ts_val) -> datetime | None:
    """Parse a timestamp string (naive assumed UTC, or offset-aware) to UTC datetime."""
    if ts_val is None:
        return None
    if isinstance(ts_val, datetime):
        return ts_val.replace(tzinfo=timezone.utc) if ts_val.tzinfo is None else ts_val
    try:
        from datetime import timezone as _tz
        import re
        s = str(ts_val).strip()
        # Normalise offset notation "+0530" → "+05:30"
        s = re.sub(r'([+-])(\d{2})(\d{2})$', r'\1\2:\3', s)
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _extract_window_data(
    cpmrn: str,
    encounter: int,
    window_start: datetime,
    window_end: datetime,
) -> tuple[list, list]:
    """
    Snapshots are cumulative — get the first snapshot taken after the window
    closed (it will contain all historical readings) and filter its vitals and
    labs to the window. Falls back to the most recent snapshot if none exists
    after window_end.
    """
    snap = _col("snapshots").find_one(
        {"CPMRN": cpmrn, "encounter": encounter, "snapshot_at": {"$gte": window_end}},
        {"chart.vitals": 1, "chart.documents": 1},
        sort=[("snapshot_at", 1)],
    )
    if snap is None:
        snap = _col("snapshots").find_one(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"chart.vitals": 1, "chart.documents": 1},
            sort=[("snapshot_at", -1)],
        )
    if snap is None:
        return [], []

    chart = snap.get("chart") or {}

    # ── Vitals ────────────────────────────────────────────────────────────────
    vitals_rows: list[dict] = []
    for v in chart.get("vitals") or []:
        ts_raw = v.get("timestamp")
        if not ts_raw:
            continue
        ts_dt = _parse_ts_utc(ts_raw)
        if ts_dt is None or not (window_start <= ts_dt <= window_end):
            continue
        # Ensure the timestamp string has a UTC marker so JS parses it correctly
        ts_out = ts_dt.isoformat()
        row: dict = {"timestamp": ts_out}
        for display, field in _VITAL_FIELDS.items():
            val = v.get(field)
            if val not in (None, "", "/", "-"):
                row[display] = val
        vitals_rows.append(row)
    vitals_rows.sort(key=lambda r: r["timestamp"])

    # ── Labs ─────────────────────────────────────────────────────────────────
    labs_rows: list[dict] = []
    for doc in chart.get("documents") or []:
        if doc.get("category") != "labs":
            continue
        reported_at = doc.get("reportedAt")
        ts_dt = _parse_ts_utc(reported_at)
        if ts_dt is None or not (window_start <= ts_dt <= window_end):
            continue
        attrs = doc.get("attributes") or {}
        labs_rows.append({
            "name":       doc.get("name") or "",
            "reportedAt": ts_dt.isoformat(),
            "values": [
                {"name": k, "value": v.get("value"), "unit": v.get("unit", "")}
                for k, v in attrs.items()
                if isinstance(v, dict) and v.get("value") is not None
            ],
        })
    labs_rows.sort(key=lambda r: r.get("reportedAt") or "")
    return vitals_rows, labs_rows


@router.post("/fn-adjudicate/{record_id}")
def adjudicate_fn(record_id: str, req: FNAdjudicationRequest, source: str = "sbar"):
    """
    Submit a reviewer verdict for a confirmed_fn SBAR or task.

    Verdicts:
      true_miss     — system should have alerted and didn't → stays in FN count
      excused       — valid reason system was silent (palliative, artefact, etc.) → removed from FN
      cooldown_miss — system detected it but cooldown blocked the alert → stays in FN, tagged
    """
    _VALID_VERDICTS = {"true_miss", "excused", "cooldown_miss"}
    if req.verdict not in _VALID_VERDICTS:
        raise HTTPException(status_code=400, detail=f"verdict must be one of {_VALID_VERDICTS}")
    if req.verdict == "excused" and not req.excusal_reason:
        raise HTTPException(status_code=400, detail="excusal_reason is required when verdict is 'excused'")

    col = _col("study_task_import") if source == "task" else _col("study_sbar_import")
    id_field = "task_id" if source == "task" else "sbar_id"
    doc = col.find_one({id_field: record_id})
    if not doc:
        raise HTTPException(status_code=404, detail=f"{source.upper()} not found")
    if doc.get("match_status") != "confirmed_fn":
        raise HTTPException(
            status_code=400,
            detail=f"Record is not in confirmed_fn state (current: {doc.get('match_status')})",
        )

    status_map = {
        "true_miss":    "fn_reviewed_miss",
        "excused":      "fn_excused",
        "cooldown_miss": "fn_cooldown_miss",
    }
    new_status = status_map[req.verdict]
    now = datetime.now(timezone.utc)

    col.update_one(
        {id_field: record_id},
        {"$set": {
            "match_status":      new_status,
            "fn_adjudicated_at": now,
            "fn_verdict":        req.verdict,
            "fn_excusal_reason": req.excusal_reason,
            "fn_reviewer_notes": req.reviewer_notes,
        }},
    )

    # Write to fn_adjudications for auditability
    _col("fn_adjudications").insert_one({
        "record_id":     record_id,
        "source":        source,
        "verdict":       req.verdict,
        "new_status":    new_status,
        "excusal_reason": req.excusal_reason,
        "reviewer_notes": req.reviewer_notes,
        "adjudicated_at": now,
        "CPMRN":         doc.get("CPMRN", ""),
        "encounter":     doc.get("encounter", 1),
    })

    logger.info(
        "study_router: FN adjudicated %s %s → %s (verdict=%s)",
        source, record_id, new_status, req.verdict,
    )

    # Return next unreviewed item (from either collection)
    next_sbar = _col("study_sbar_import").find_one(
        {"match_status": "confirmed_fn", "sbar_id": {"$ne": record_id if source == "sbar" else None}},
        sort=[("create_date_time", 1)],
    )
    next_task = _col("study_task_import").find_one(
        {"match_status": "confirmed_fn", "task_id": {"$ne": record_id if source == "task" else None}},
        sort=[("task_visible_at", 1)],
    )
    # Return whichever is oldest
    next_record = None
    next_source = None
    if next_sbar and next_task:
        s_time = next_sbar.get("create_date_time") or datetime.min.replace(tzinfo=timezone.utc)
        t_time = next_task.get("task_visible_at")   or datetime.min.replace(tzinfo=timezone.utc)
        if s_time <= t_time:
            next_record, next_source = next_sbar["sbar_id"], "sbar"
        else:
            next_record, next_source = next_task["task_id"], "task"
    elif next_sbar:
        next_record, next_source = next_sbar["sbar_id"], "sbar"
    elif next_task:
        next_record, next_source = next_task["task_id"], "task"

    return {
        "ok":           True,
        "record_id":    record_id,
        "source":       source,
        "new_status":   new_status,
        "next_record_id": next_record,
        "next_source":    next_source,
    }


@router.get("/fn-metrics")
def get_fn_metrics(study_id: Optional[str] = None):
    """Return live FN breakdown counts for the FN review panel."""
    sbar_col = _col("study_sbar_import")
    task_col = _col("study_task_import")

    sbar_time: dict = {}
    task_time: dict = {}
    if study_id:
        start_dt, end_dt = _resolve_time_window(study_id)
        sbar_time = {"create_date_time": {"$gte": start_dt}}
        task_time = {"task_visible_at":  {"$gte": start_dt}}
        if end_dt:
            sbar_time["create_date_time"]["$lte"] = end_dt
            task_time["task_visible_at"]["$lte"]  = end_dt

    def _count(col, status, time_q):
        return col.count_documents({"match_status": status, **time_q})

    fn_unreviewed   = (_count(sbar_col, "confirmed_fn",    sbar_time) + _count(task_col, "confirmed_fn",    task_time))
    fn_true_miss    = (_count(sbar_col, "fn_reviewed_miss",sbar_time) + _count(task_col, "fn_reviewed_miss",task_time))
    fn_cooldown     = (_count(sbar_col, "fn_cooldown_miss",sbar_time) + _count(task_col, "fn_cooldown_miss",task_time))
    fn_excused      = (_count(sbar_col, "fn_excused",      sbar_time) + _count(task_col, "fn_excused",      task_time))
    fn_total_in_count = fn_unreviewed + fn_true_miss

    return {
        "fn_unreviewed":        fn_unreviewed,
        "fn_true_miss":         fn_true_miss,
        "fn_cooldown_miss":     fn_cooldown,
        "fn_excused":           fn_excused,
        "fn_total_in_count":    fn_total_in_count,
        "fn_total_adjudicated": fn_true_miss + fn_cooldown + fn_excused,
    }


# ── Study CRUD ─────────────────────────────────────────────────────────────────

@router.get("/studies")
def list_studies():
    """Return all study definitions, newest first."""
    studies = list(_col("studies").find({}).sort("start_dt", -1))
    return {"studies": [_ser(s) for s in studies], "count": len(studies)}


@router.post("/studies")
def create_study(req: StudyCreate):
    """Create a new study period."""
    if req.status not in ("active", "completed", "draft"):
        raise HTTPException(status_code=400, detail="status must be active, completed, or draft")
    try:
        start_dt = _parse_iso(req.start_dt)
    except Exception:
        raise HTTPException(status_code=400, detail="start_dt must be a valid ISO 8601 datetime")
    end_dt = None
    if req.end_dt:
        try:
            end_dt = _parse_iso(req.end_dt)
        except Exception:
            raise HTTPException(status_code=400, detail="end_dt must be a valid ISO 8601 datetime")
    if end_dt and end_dt <= start_dt:
        raise HTTPException(status_code=400, detail="end_dt must be after start_dt")

    study_id = "s_" + _uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    doc = {
        "study_id":   study_id,
        "name":       req.name.strip(),
        "start_dt":   start_dt,
        "end_dt":     end_dt,
        "status":     req.status,
        "created_at": now,
        "updated_at": now,
    }
    _col("studies").insert_one(doc)
    logger.info("study_router: created study %s (%s)", study_id, req.name)
    return {"ok": True, "study": _ser(doc)}


@router.get("/studies/{study_id}")
def get_study(study_id: str):
    """Return a single study."""
    study = _col("studies").find_one({"study_id": study_id})
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")
    return {"study": _ser(study)}


@router.put("/studies/{study_id}")
def update_study(study_id: str, req: StudyUpdate):
    """Update a study's name, dates, or status."""
    study = _col("studies").find_one({"study_id": study_id})
    if not study:
        raise HTTPException(status_code=404, detail="Study not found")

    patch: dict = {"updated_at": datetime.now(timezone.utc)}
    if req.name is not None:
        patch["name"] = req.name.strip()
    if req.status is not None:
        if req.status not in ("active", "completed", "draft"):
            raise HTTPException(status_code=400, detail="status must be active, completed, or draft")
        patch["status"] = req.status
    if req.start_dt is not None:
        try:
            patch["start_dt"] = _parse_iso(req.start_dt)
        except Exception:
            raise HTTPException(status_code=400, detail="start_dt must be a valid ISO 8601 datetime")
    if req.end_dt is not None:
        try:
            patch["end_dt"] = _parse_iso(req.end_dt)
        except Exception:
            raise HTTPException(status_code=400, detail="end_dt must be a valid ISO 8601 datetime")

    _col("studies").update_one({"study_id": study_id}, {"$set": patch})
    updated = _col("studies").find_one({"study_id": study_id})
    return {"ok": True, "study": _ser(updated)}


@router.delete("/studies/{study_id}")
def delete_study(study_id: str):
    """Delete a study definition (does not touch any alert/snapshot data)."""
    result = _col("studies").delete_one({"study_id": study_id})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Study not found")
    logger.info("study_router: deleted study %s", study_id)
    return {"ok": True, "study_id": study_id}


@router.get("/costs/summary")
def get_costs_summary():
    """Aggregate cost summary across all runs (for finance approval)."""
    pipeline = [
        {"$group": {
            "_id": None,
            "total_cost_usd":      {"$sum": "$totals.cost_usd"},
            "total_input_tokens":  {"$sum": "$totals.input_tokens"},
            "total_output_tokens": {"$sum": "$totals.output_tokens"},
            "total_thinking_tokens": {"$sum": "$totals.thinking_tokens"},
            "run_count":           {"$sum": 1},
            "first_run":           {"$min": "$run_started_at"},
            "last_run":            {"$max": "$run_started_at"},
        }},
    ]
    result = list(_col("pipeline_run_costs").aggregate(pipeline))
    if not result:
        return {"summary": None}

    agg = result[0]
    agg.pop("_id", None)
    # Compute average cost per run
    if agg.get("run_count"):
        agg["avg_cost_per_run_usd"] = round(agg["total_cost_usd"] / agg["run_count"], 6)
        agg["est_monthly_cost_usd"] = round(agg["avg_cost_per_run_usd"] * 24 * 30, 2)
        agg["est_daily_cost_usd"]   = round(agg["avg_cost_per_run_usd"] * 24, 2)

    # Convert datetimes
    for k in ("first_run", "last_run"):
        if isinstance(agg.get(k), datetime):
            agg[k] = agg[k].isoformat()

    return {"summary": agg, "pricing_model": "gemini-2.5-flash"}
