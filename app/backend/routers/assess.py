"""
Assessment endpoints — auto-generate questions on ingest, run them through
chat, and track knowledge-gap registrations as a measure of improvement.

POST /api/assess/generate          — generate 10 questions for a source (background)
GET  /api/assess/                  — list all assessments (headers only)
GET  /api/assess/jobs/{job_id}     — poll job status
GET  /api/assess/{source_slug}     — load full assessment
POST /api/assess/{source_slug}/run — run all 10 questions (background)
POST /api/assess/{source_slug}/rate — submit user rating for one question
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.assess_pipeline import (
    generate_assessment,
    list_assessments,
    load_assessment,
    rate_question,
    run_assessment,
    update_question_text,
)

log = logging.getLogger("wiki.assess")

router = APIRouter(prefix="/api/assess", tags=["assess"])

_jobs: dict[str, dict] = {}


# ── Request models ─────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    source_slug:    str
    source_name:    str
    ingest_summary: str
    knowledge_gaps: list[dict] = []
    files_written:  list[str]  = []


class RateRequest(BaseModel):
    question_id: int
    rating:      bool | None


class EditQuestionRequest(BaseModel):
    question_id: int
    question:    str


# ── Endpoints ──────────────────────────────────────────────────────────────────

@router.get("/")
def list_assessments_endpoint(kb: KBConfig = Depends(resolve_kb)):
    return {"assessments": list_assessments(kb)}


# IMPORTANT: /jobs/{job_id} must be registered before /{source_slug}
@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


@router.get("/{source_slug}")
def get_assessment(source_slug: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        return load_assessment(source_slug, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment for {source_slug!r}")


@router.post("/generate")
async def generate_assessment_endpoint(
    req: GenerateRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "running",
        "source_slug": req.source_slug,
        "result": None,
        "error": None,
    }
    background_tasks.add_task(
        _do_generate,
        job_id, req.source_slug, req.source_name,
        req.ingest_summary, req.knowledge_gaps, req.files_written,
        kb,
    )
    return {"job_id": job_id}


@router.post("/{source_slug}/run")
async def run_assessment_endpoint(
    source_slug: str,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    # Verify the assessment exists before kicking off the job
    try:
        load_assessment(source_slug, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment for {source_slug!r}")

    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {
        "status": "running",
        "source_slug": source_slug,
        "result": None,
        "error": None,
    }
    background_tasks.add_task(_do_run, job_id, source_slug, kb)
    return {"job_id": job_id}


@router.patch("/{source_slug}/questions")
def edit_question_endpoint(
    source_slug: str,
    req: EditQuestionRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    try:
        return update_question_text(source_slug, req.question_id, req.question, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment for {source_slug!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/{source_slug}/rate")
def rate_question_endpoint(
    source_slug: str,
    req: RateRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    try:
        assessment = rate_question(source_slug, req.question_id, req.rating, kb)
        return assessment
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"No assessment for {source_slug!r}")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


# ── Background tasks ───────────────────────────────────────────────────────────

async def _do_generate(
    job_id: str,
    source_slug: str,
    source_name: str,
    ingest_summary: str,
    knowledge_gaps: list[dict],
    files_written: list[str],
    kb: KBConfig,
):
    try:
        result = await asyncio.to_thread(
            generate_assessment,
            source_slug, source_name, ingest_summary, knowledge_gaps, files_written, kb,
        )
        _jobs[job_id] = {
            "status": "done",
            "source_slug": source_slug,
            "result": result,
            "error": None,
        }
    except Exception as exc:
        log.error("Assessment generation failed for %r: %s", source_slug, exc)
        _jobs[job_id] = {
            "status": "error",
            "source_slug": source_slug,
            "result": None,
            "error": str(exc),
        }


async def _do_run(job_id: str, source_slug: str, kb: KBConfig):
    try:
        result = await asyncio.to_thread(run_assessment, source_slug, kb)
        _jobs[job_id] = {
            "status": "done",
            "source_slug": source_slug,
            "result": result,
            "error": None,
        }
    except Exception as exc:
        log.error("Assessment run failed for %r: %s", source_slug, exc)
        _jobs[job_id] = {
            "status": "error",
            "source_slug": source_slug,
            "result": None,
            "error": str(exc),
        }
