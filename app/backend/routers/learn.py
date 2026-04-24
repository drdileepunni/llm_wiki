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
from ..cancellation import cancel_run, get_stop_event
from ..services.learn_pipeline import _load_run, list_learn_runs, start_learn_run

log = logging.getLogger("wiki.learn")

router = APIRouter(prefix="/api/learn", tags=["learn"])

_jobs: dict[str, dict] = {}


class StartRequest(BaseModel):
    cpmrn:     str
    encounter: str


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
    background_tasks.add_task(_do_run, run_id, req.cpmrn, req.encounter, kb, stop_event)
    return {"run_id": run_id}


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


async def _do_run(run_id: str, cpmrn: str, encounter: str, kb: KBConfig, stop_event=None):
    try:
        await asyncio.to_thread(start_learn_run, cpmrn, encounter, kb, run_id, stop_event)
        _jobs[run_id] = {"status": "done"}
    except Exception as exc:
        log.error("Learn run %s failed: %s", run_id, exc)
        _jobs[run_id] = {"status": "error", "error": str(exc)}
