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
from datetime import datetime, timezone
from pathlib import Path

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
    verdict:              str   # "Appropriate" | "Inappropriate"
    explainability_rating: int  # 1=clear, 2=partial, 3=unclear
    reviewer_notes:       str = ""


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/queue")
def get_queue():
    """Return all fp_candidate alerts pending adjudication, oldest first."""
    alerts = list(
        _col("study_alerts")
        .find({"match_status": "fp_candidate"})
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

    # How many hours until the match window closes (if still pending)
    window_open_hours = None
    if alert.get("match_status") in ("pending", "fp_candidate"):
        # Find any SBAR that could still match (same patient, window still open)
        from backend.services.emr.db import get_db
        now = datetime.now(timezone.utc)
        alerted_at = alert.get("alerted_at")
        if isinstance(alerted_at, datetime) and alerted_at.tzinfo is None:
            alerted_at = alerted_at.replace(tzinfo=timezone.utc)
        if alerted_at:
            # Time remaining before we declare this a confirmed FP (no auto-mechanism
            # but gives the reviewer context on urgency)
            elapsed_h = (now - alerted_at).total_seconds() / 3600
            window_open_hours = round(max(0, 8 - elapsed_h), 1)
    result["window_open_hours"] = window_open_hours

    return result


@router.post("/adjudicate/{alert_id}")
def adjudicate(alert_id: str, req: AdjudicationRequest):
    """Submit a reviewer verdict for an alert."""
    if req.verdict not in ("Appropriate", "Inappropriate"):
        raise HTTPException(status_code=400, detail="verdict must be 'Appropriate' or 'Inappropriate'")
    if req.explainability_rating not in (1, 2, 3):
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
        "study_router: adjudicated alert %s → %s (expl=%d)",
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


@router.get("/metrics")
def get_metrics():
    """Return the most recent metrics snapshot."""
    snap = _col("study_metrics_snapshots").find_one(
        {}, sort=[("computed_at", -1)]
    )
    if not snap:
        return {"metrics": None}
    return {"metrics": _ser(snap)}


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
