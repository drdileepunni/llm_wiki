"""
Snapshot collection endpoints.

GET  /api/snapshots/schedule              — list scheduled patients
POST /api/snapshots/schedule              — add patient to hourly schedule
DELETE /api/snapshots/schedule/{cpmrn}/{encounter} — remove from schedule
POST /api/snapshots/collect               — collect now for a CPMRN
GET  /api/snapshots/scheduler/status      — APScheduler next run, last run
"""
import sys
import logging
from datetime import datetime, timezone
from pathlib import Path
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/snapshots", tags=["snapshots"])
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


# ── Request models ─────────────────────────────────────────────────────────

class ScheduleRequest(BaseModel):
    cpmrn: str
    encounter: int = 1
    workspace: str | None = None   # used to verify admission before each collection

class CollectRequest(BaseModel):
    cpmrn: str
    encounter: int = 1

class WorkspaceRequest(BaseModel):
    workspace: str = "1A"
    schedule: bool = False   # if True, also add each patient to hourly schedule


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_db():
    from backend.services.emr.db import get_db
    return get_db()


def _fmt(dt):
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        from datetime import timezone as _tz
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_tz.utc)
        return dt.isoformat()
    return str(dt)


# ── Schedule management ────────────────────────────────────────────────────

@router.get("/schedule")
def list_schedule():
    db = _get_db()
    docs = list(db.snapshot_schedule.find({}, {"_id": 0}).sort("added_at", 1))
    for d in docs:
        d["added_at"]        = _fmt(d.get("added_at"))
        d["last_collected_at"] = _fmt(d.get("last_collected_at"))
    return {"scheduled": docs}


@router.post("/schedule")
def add_to_schedule(body: ScheduleRequest):
    db = _get_db()
    existing = db.snapshot_schedule.find_one(
        {"CPMRN": body.cpmrn, "encounter": body.encounter}
    )
    if existing:
        update = {"active": True}
        if body.workspace:
            update["workspace"] = body.workspace
        db.snapshot_schedule.update_one(
            {"CPMRN": body.cpmrn, "encounter": body.encounter},
            {"$set": update}
        )
        return {"status": "already_scheduled", "cpmrn": body.cpmrn, "encounter": body.encounter}

    db.snapshot_schedule.insert_one({
        "CPMRN":             body.cpmrn,
        "encounter":         body.encounter,
        "workspace":         body.workspace,
        "active":            True,
        "added_at":          datetime.now(timezone.utc),
        "last_collected_at": None,
        "last_error":        None,
    })
    return {"status": "scheduled", "cpmrn": body.cpmrn, "encounter": body.encounter}


@router.delete("/schedule/{cpmrn}/{encounter}")
def remove_from_schedule(cpmrn: str, encounter: int):
    db = _get_db()
    result = db.snapshot_schedule.delete_one({"CPMRN": cpmrn, "encounter": encounter})
    if result.deleted_count == 0:
        raise HTTPException(404, "Patient not in schedule")
    return {"status": "removed", "cpmrn": cpmrn, "encounter": encounter}


# ── Delete a snapshot ─────────────────────────────────────────────────────

@router.delete("/snapshots/{snapshot_id}")
def delete_snapshot(snapshot_id: str):
    from bson import ObjectId
    db = _get_db()
    try:
        oid = ObjectId(snapshot_id)
    except Exception:
        raise HTTPException(400, "Invalid snapshot ID")
    result = db.snapshots.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(404, "Snapshot not found")
    return {"status": "deleted", "id": snapshot_id}


# ── Collect now ────────────────────────────────────────────────────────────

@router.post("/collect")
def collect_now(body: CollectRequest):
    try:
        from tools.radar_sync.snapshot_collector import collect
        result = collect(cpmrn=body.cpmrn, encounter=body.encounter)

        # Update last_collected_at if this patient is scheduled
        db = _get_db()
        db.snapshot_schedule.update_one(
            {"CPMRN": body.cpmrn, "encounter": body.encounter},
            {"$set": {"last_collected_at": datetime.now(timezone.utc), "last_error": None}},
        )
        return {"status": "collected", **result}
    except Exception as e:
        logger.exception("collect_now failed for %s", body.cpmrn)
        raise HTTPException(500, str(e))


# ── Workspace collect ──────────────────────────────────────────────────────

@router.post("/collect-workspace")
def collect_workspace(body: WorkspaceRequest):
    """Collect snapshots for all currently admitted patients in a workspace."""
    try:
        from tools.radar_sync.chart_puller import get_admitted_patients
        from tools.radar_sync.snapshot_collector import collect

        patients = get_admitted_patients(body.workspace)
        if not patients:
            return {"workspace": body.workspace, "patients_found": 0, "results": []}

        db = _get_db()
        results = []
        for p in patients:
            cpmrn    = p["CPMRN"]
            encounter = p.get("encounter", 1)
            try:
                result = collect(cpmrn=cpmrn, encounter=encounter)
                results.append({"cpmrn": cpmrn, "encounter": encounter, "status": "collected", **result})

                if body.schedule:
                    existing = db.snapshot_schedule.find_one({"CPMRN": cpmrn, "encounter": encounter})
                    if existing:
                        db.snapshot_schedule.update_one(
                            {"CPMRN": cpmrn, "encounter": encounter},
                            {"$set": {"active": True}}
                        )
                    else:
                        db.snapshot_schedule.insert_one({
                            "CPMRN":             cpmrn,
                            "encounter":         encounter,
                            "workspace":         body.workspace,
                            "active":            True,
                            "added_at":          datetime.now(timezone.utc),
                            "last_collected_at": datetime.now(timezone.utc),
                            "last_error":        None,
                        })
            except Exception as e:
                logger.exception("collect_workspace: failed for %s", cpmrn)
                results.append({"cpmrn": cpmrn, "encounter": encounter, "status": "error", "error": str(e)})

        return {"workspace": body.workspace, "patients_found": len(patients), "results": results}
    except Exception as e:
        logger.exception("collect_workspace failed")
        raise HTTPException(500, str(e))


# ── Scheduler status / pause / resume ─────────────────────────────────────

@router.get("/scheduler/status")
def scheduler_status():
    from backend.scheduler import get_scheduler_status
    return get_scheduler_status()

@router.post("/scheduler/pause")
def pause_scheduler():
    from backend.scheduler import get_scheduler_status
    import backend.scheduler as sched
    if sched._scheduler and sched._scheduler.running:
        sched._scheduler.pause()
    return {**get_scheduler_status(), "paused": True}

@router.post("/scheduler/resume")
def resume_scheduler():
    from backend.scheduler import get_scheduler_status
    import backend.scheduler as sched
    if sched._scheduler and sched._scheduler.running:
        sched._scheduler.resume()
    return {**get_scheduler_status(), "paused": False}
