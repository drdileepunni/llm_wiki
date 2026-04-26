"""
Learning loop endpoints.

POST /api/learn/start        — { cpmrn, encounter } → { run_id }
GET  /api/learn/jobs/{id}    — poll run state
GET  /api/learn/             — list all runs (summary)
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..cancellation import cancel_run, get_stop_event, resume_run
from ..services.learn_pipeline import _load_run, _run_path, list_learn_runs, start_learn_run, resume_learn_run

log = logging.getLogger("wiki.learn")

router = APIRouter(prefix="/api/learn", tags=["learn"])

_jobs: dict[str, dict] = {}


class StartRequest(BaseModel):
    cpmrn:            str
    encounter:        str
    num_snapshots:    int  = 2
    review_questions: bool = True


class ResumeRequest(BaseModel):
    questions: list[dict] = []  # updated questions from the user; empty = approve as-is


@router.get("/")
def list_runs(kb: KBConfig = Depends(resolve_kb)):
    return {"runs": list_learn_runs(kb)}


# IMPORTANT: /jobs/{run_id} must be before any wildcard route
@router.get("/jobs/{run_id}")
def get_job(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    # Disk is the source of truth — the pipeline writes state at every phase
    try:
        return _load_run(run_id, kb)
    except FileNotFoundError:
        # File not written yet (job just started) or never existed
        if run_id in _jobs:
            return {"run_id": run_id, "status": "running", "log": []}
        return {"run_id": run_id, "status": "not_found", "log": []}


@router.post("/start")
async def start(
    req: StartRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    run_id = str(uuid.uuid4())[:8]
    _jobs[run_id] = {"status": "running"}
    stop_event = get_stop_event(run_id)
    background_tasks.add_task(_do_run, run_id, req.cpmrn, req.encounter, kb, stop_event, req.num_snapshots, req.review_questions)
    return {"run_id": run_id}


@router.post("/jobs/{run_id}/resume")
def resume_job(run_id: str, req: ResumeRequest, kb: KBConfig = Depends(resolve_kb)):
    """Unblock the question-review gate. Optionally persist edited questions first."""
    try:
        state = _load_run(run_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

    if state.get("current_phase") != "pending_review":
        raise HTTPException(status_code=400, detail="Run is not pending review")

    # If the user submitted edited questions, persist them to the assessment file
    if req.questions:
        import json as _json
        from ..services.assess_pipeline import _assessment_path, load_assessment
        try:
            assess_data = load_assessment(state["slug"], kb)
            # Replace questions with edited versions (matched by id)
            edited_by_id = {q["id"]: q for q in req.questions}
            for q in assess_data.get("questions", []):
                if q["id"] in edited_by_id:
                    q["question"] = edited_by_id[q["id"]]["question"]
            p = _assessment_path(state["slug"], kb)
            p.write_text(_json.dumps(assess_data, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to persist edited questions: %s", exc)

    found = resume_run(run_id)
    return {"run_id": run_id, "resumed": found}


@router.post("/jobs/{run_id}/cancel")
def cancel_job(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    found = cancel_run(run_id)
    if not found:
        # Server may have restarted — forcibly mark the run as stopped on disk
        try:
            import json
            from datetime import datetime
            state = _load_run(run_id, kb)
            if state.get("status") == "running":
                state["status"] = "stopped"
                state["current_phase"] = "stopped"
                state["completed_at"] = datetime.utcnow().isoformat()
                state.setdefault("log", []).append({
                    "phase": "stopped",
                    "message": "Run stopped by user (server restart recovery)",
                    "timestamp": datetime.utcnow().isoformat(),
                })
                from ..services.learn_pipeline import _save_run
                _save_run(state, kb)
                found = True
        except FileNotFoundError:
            pass
    return {"run_id": run_id, "cancelled": found}


@router.post("/jobs/{run_id}/restart")
async def restart_job(
    run_id: str,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    """Resume a stopped/error run from the knowledge-loop iteration where it stopped."""
    try:
        state = _load_run(run_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")

    if state.get("status") not in ("stopped", "error"):
        raise HTTPException(status_code=400, detail=f"Run {run_id!r} is not stopped")

    stop_event = get_stop_event(run_id)
    _jobs[run_id] = {"status": "running"}
    background_tasks.add_task(_do_resume, run_id, kb, stop_event)
    return {"run_id": run_id, "restarted": True}


@router.delete("/jobs/{run_id}")
def delete_job(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    p = _run_path(run_id, kb)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Run {run_id!r} not found")
    p.unlink()
    _jobs.pop(run_id, None)
    return {"run_id": run_id, "deleted": True}


async def _do_run(run_id: str, cpmrn: str, encounter: str, kb: KBConfig, stop_event=None, num_snapshots: int = 2, review_questions: bool = True):
    try:
        await asyncio.to_thread(start_learn_run, cpmrn, encounter, kb, run_id, stop_event, num_snapshots, review_questions)
        _jobs[run_id] = {"status": "done"}
    except Exception as exc:
        log.error("Learn run %s failed: %s", run_id, exc)
        _jobs[run_id] = {"status": "error", "error": str(exc)}


async def _do_resume(run_id: str, kb: KBConfig, stop_event=None):
    try:
        await asyncio.to_thread(resume_learn_run, run_id, kb, stop_event)
        _jobs[run_id] = {"status": "done"}
    except Exception as exc:
        log.error("Resumed learn run %s failed: %s", run_id, exc)
        _jobs[run_id] = {"status": "error", "error": str(exc)}
