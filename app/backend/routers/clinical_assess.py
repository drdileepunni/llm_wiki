"""
Clinical case assessment endpoints.

POST /api/clinical-assess/run                                   — run all snapshots (background)
GET  /api/clinical-assess/jobs/{job_id}                         — poll job status
GET  /api/clinical-assess/                                      — list all runs (all patients)
GET  /api/clinical-assess/{patient_id}/{run_id}                 — load one run
PATCH /api/clinical-assess/{patient_id}/{run_id}/snapshots/{n}/rating
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.clinical_assess_pipeline import (
    delete_clinical_assessment,
    list_available_patients,
    list_clinical_assessments,
    list_patient_snapshot_info,
    load_clinical_assessment,
    run_clinical_assessment,
    save_run_comment,
    save_snapshot_rating,
)

log = logging.getLogger("wiki.clinical_assess")

router = APIRouter(prefix="/api/clinical-assess", tags=["clinical-assess"])

_jobs: dict[str, dict] = {}


class RunRequest(BaseModel):
    patient_id: str
    model: str | None = None
    reasoning_model: str | None = None
    snapshot_num: int | None = None  # None = run all
    use_patient_context: bool = False


class RatingRequest(BaseModel):
    rating: int | None = None
    knowledge_gaps: list[str] | None = None

class CommentRequest(BaseModel):
    comment: str = ""


@router.get("/available")
def available_patients():
    return {"patients": list_available_patients()}


@router.get("/patients/{patient_id}/snapshots")
def patient_snapshots(patient_id: str):
    return {"snapshots": list_patient_snapshot_info(patient_id)}


@router.get("/")
def list_endpoint(kb: KBConfig = Depends(resolve_kb)):
    return {"assessments": list_clinical_assessments(kb)}


# IMPORTANT: /jobs/{job_id} must stay above /{patient_id}/...
@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


@router.patch("/{patient_id}/{run_id}/snapshots/{snapshot_num}/rating")
def rate_snapshot(
    patient_id: str,
    run_id: str,
    snapshot_num: int,
    req: RatingRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    if req.rating is not None and not 1 <= req.rating <= 10:
        raise HTTPException(status_code=422, detail="Rating must be between 1 and 10")
    try:
        return save_snapshot_rating(patient_id, run_id, snapshot_num, req.rating, kb, req.knowledge_gaps)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment {run_id!r} for {patient_id!r}")
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.patch("/{patient_id}/{run_id}/comment")
def save_comment(
    patient_id: str,
    run_id: str,
    req: CommentRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    try:
        return save_run_comment(patient_id, run_id, req.comment, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment {run_id!r} for {patient_id!r}")


@router.get("/{patient_id}/{run_id}")
def get_assessment(patient_id: str, run_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        return load_clinical_assessment(patient_id, run_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment {run_id!r} for {patient_id!r}")


@router.delete("/{patient_id}/{run_id}")
def delete_assessment(patient_id: str, run_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        delete_clinical_assessment(patient_id, run_id, kb)
        return {"deleted": True}
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment {run_id!r} for {patient_id!r}")


@router.post("/run")
async def run_endpoint(
    req: RunRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "patient_id": req.patient_id, "result": None, "error": None}
    background_tasks.add_task(_do_run, job_id, req.patient_id, kb, req.model, req.reasoning_model, req.snapshot_num, req.use_patient_context)
    return {"job_id": job_id}


async def _do_run(job_id: str, patient_id: str, kb: KBConfig, model: str | None = None, reasoning_model: str | None = None, snapshot_num: int | None = None, use_patient_context: bool = False):
    try:
        result = await asyncio.to_thread(run_clinical_assessment, patient_id, kb, model, snapshot_num, use_patient_context, reasoning_model)
        _jobs[job_id] = {"status": "done", "patient_id": patient_id, "result": result, "error": None}
    except Exception as exc:
        log.error("Clinical assessment failed for %r: %s", patient_id, exc)
        _jobs[job_id] = {"status": "error", "patient_id": patient_id, "result": None, "error": str(exc)}
