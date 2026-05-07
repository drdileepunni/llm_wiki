"""
Viva batch run endpoints.

POST   /api/viva-batch/              — start a new batch run
GET    /api/viva-batch/              — list all batch runs
GET    /api/viva-batch/catalog       — return the scenario catalog (diagnoses × complications)
GET    /api/viva-batch/{run_id}      — poll a batch run's live state
POST   /api/viva-batch/{run_id}/cancel
DELETE /api/viva-batch/{run_id}

NOTE: prefix is /api/viva-batch (not /api/viva/batch) to avoid FastAPI's
/{session_id} catch-all in the viva router matching "batch" as a session ID.
"""

import asyncio
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..cancellation import cancel_run, get_stop_event
from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.scenario_catalog import get_catalog_stats
from ..services.viva_batch_pipeline import (
    _batch_run_path,
    _load_batch,
    list_batch_runs,
    start_viva_batch,
    continue_viva_batch,
)

log = logging.getLogger("wiki.viva_batch")

router = APIRouter(prefix="/api/viva-batch", tags=["viva-batch"])

_jobs: dict[str, dict] = {}


# ── Pydantic models ────────────────────────────────────────────────────────────

class BatchStartRequest(BaseModel):
    n_sessions:  int         = 10
    mode:        str         = "weighted"   # "weighted" | "random" | "full"
    iterations:  int         = 3
    max_turns:   int         = 6
    model:       str | None  = None
    seed:        int | None  = None


class ExtendRequest(BaseModel):
    additional_iterations: int = 3


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/catalog")
def get_catalog():
    """Return the diagnosis × complication catalog for display in the UI."""
    return get_catalog_stats()


@router.get("/")
def list_runs(kb: KBConfig = Depends(resolve_kb)):
    return {"runs": list_batch_runs(kb)}


@router.post("/")
async def start_batch(
    req: BatchStartRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    if req.mode not in ("weighted", "random", "full"):
        raise HTTPException(status_code=400, detail="mode must be 'weighted', 'random', or 'full'")
    if req.iterations < 1 or req.iterations > 20:
        raise HTTPException(status_code=400, detail="iterations must be between 1 and 20")
    if req.n_sessions < 1 or req.n_sessions > 210:
        raise HTTPException(status_code=400, detail="n_sessions must be between 1 and 210")

    run_id = f"vbatch_{uuid.uuid4().hex[:8]}"
    stop_event = get_stop_event(run_id)
    _jobs[run_id] = {"status": "running"}
    background_tasks.add_task(_do_batch_run, run_id, req, kb, stop_event)
    return {"run_id": run_id}


@router.get("/{run_id}")
def get_run(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    try:
        return _load_batch(run_id, kb)
    except FileNotFoundError:
        if run_id in _jobs:
            return {"run_id": run_id, "status": "running", "log": [], "metrics": []}
        raise HTTPException(status_code=404, detail=f"Batch run {run_id!r} not found")


@router.post("/{run_id}/cancel")
def cancel_batch(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    found = cancel_run(run_id)
    if not found:
        try:
            from datetime import datetime
            state = _load_batch(run_id, kb)
            if state.get("status") == "running":
                from ..services.viva_batch_pipeline import _save_batch
                state["status"] = "stopped"
                state["current_phase"] = "stopped"
                state["completed_at"] = datetime.utcnow().isoformat()
                state.setdefault("log", []).append({
                    "phase": "stopped",
                    "message": "Batch run stopped by user (server restart recovery)",
                    "timestamp": datetime.utcnow().isoformat(),
                })
                _save_batch(state, kb)
                found = True
        except FileNotFoundError:
            pass
    return {"run_id": run_id, "cancelled": found}


@router.post("/{run_id}/extend")
async def extend_batch(
    run_id: str,
    req: ExtendRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    if req.additional_iterations < 1 or req.additional_iterations > 20:
        raise HTTPException(status_code=400, detail="additional_iterations must be between 1 and 20")
    try:
        state = _load_batch(run_id, kb)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"Batch run {run_id!r} not found")
    if state.get("status") == "running":
        raise HTTPException(status_code=409, detail="Batch run is already running")

    stop_event = get_stop_event(run_id)
    _jobs[run_id] = {"status": "running"}
    background_tasks.add_task(_do_extend_run, run_id, req.additional_iterations, kb, stop_event)
    return {"run_id": run_id, "additional_iterations": req.additional_iterations}


@router.delete("/{run_id}")
def delete_run(run_id: str, kb: KBConfig = Depends(resolve_kb)):
    p = _batch_run_path(run_id, kb)
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"Batch run {run_id!r} not found")
    p.unlink()
    _jobs.pop(run_id, None)
    return {"run_id": run_id, "deleted": True}


# ── Background task wrapper ───────────────────────────────────────────────────

async def _do_extend_run(
    run_id: str,
    additional_iterations: int,
    kb: KBConfig,
    stop_event,
):
    try:
        await asyncio.to_thread(continue_viva_batch, run_id, additional_iterations, kb, stop_event)
        _jobs[run_id] = {"status": "done"}
    except Exception as exc:
        log.error("Batch extend %s failed: %s", run_id, exc)
        _jobs[run_id] = {"status": "error", "error": str(exc)}


async def _do_batch_run(
    run_id: str,
    req: BatchStartRequest,
    kb: KBConfig,
    stop_event,
):
    try:
        await asyncio.to_thread(
            start_viva_batch,
            req.n_sessions,
            req.mode,
            req.iterations,
            req.max_turns,
            req.model,
            kb,
            run_id,
            stop_event,
            req.seed,
        )
        _jobs[run_id] = {"status": "done"}
    except Exception as exc:
        log.error("Batch run %s failed: %s", run_id, exc)
        _jobs[run_id] = {"status": "error", "error": str(exc)}
