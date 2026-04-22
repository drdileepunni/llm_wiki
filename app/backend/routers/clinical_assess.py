"""
Clinical case assessment endpoints.

POST /api/clinical-assess/run          — run all snapshots for a patient dir (background)
GET  /api/clinical-assess/jobs/{id}    — poll job status
GET  /api/clinical-assess/             — list completed clinical assessments
GET  /api/clinical-assess/{patient_id} — load one assessment
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.clinical_assess_pipeline import (
    list_clinical_assessments,
    load_clinical_assessment,
    run_clinical_assessment,
)

log = logging.getLogger("wiki.clinical_assess")

router = APIRouter(prefix="/api/clinical-assess", tags=["clinical-assess"])

_jobs: dict[str, dict] = {}


class RunRequest(BaseModel):
    patient_dir: str


@router.get("/")
def list_endpoint(kb: KBConfig = Depends(resolve_kb)):
    return {"assessments": list_clinical_assessments(kb)}


# IMPORTANT: /jobs/{job_id} must be before /{patient_id}
@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


@router.get("/{patient_id}")
def get_assessment(patient_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        return load_clinical_assessment(patient_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No clinical assessment for {patient_id!r}")


@router.post("/run")
async def run_endpoint(
    req: RunRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "patient_dir": req.patient_dir, "result": None, "error": None}
    background_tasks.add_task(_do_run, job_id, req.patient_dir, kb)
    return {"job_id": job_id}


async def _do_run(job_id: str, patient_dir: str, kb: KBConfig):
    try:
        result = await asyncio.to_thread(run_clinical_assessment, patient_dir, kb)
        _jobs[job_id] = {"status": "done", "patient_dir": patient_dir, "result": result, "error": None}
    except Exception as exc:
        log.error("Clinical assessment failed for %r: %s", patient_dir, exc)
        _jobs[job_id] = {"status": "error", "patient_dir": patient_dir, "result": None, "error": str(exc)}
