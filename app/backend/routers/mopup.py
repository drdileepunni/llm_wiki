"""
Mop-up endpoints.

POST /api/mopup/run          — start an async mop-up job, returns job_id immediately
GET  /api/mopup/jobs/{id}    — poll job status / result
GET  /api/mopup/queue        — preview stub queue (no LLM, no writes)
"""

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.mopup_pipeline import (
    SCORE_THRESHOLD,
    WORD_THRESHOLD,
    MAX_STUBS,
    _VALID_SUBTYPES,
    get_stub_queue,
    mopup_run,
    set_page_subtype,
    infer_subtype_llm,
)

log = logging.getLogger("wiki.mopup")

router = APIRouter(prefix="/api/mopup", tags=["mopup"])

_jobs: dict[str, dict] = {}


# ── Request model ─────────────────────────────────────────────────────────────

class MopupRequest(BaseModel):
    score_threshold: int  = SCORE_THRESHOLD
    word_threshold:  int  = WORD_THRESHOLD
    max_stubs:       int  = MAX_STUBS
    run_defrag:      bool = True

class SubtypeRequest(BaseModel):
    page:    str
    subtype: str


# ── Background worker ─────────────────────────────────────────────────────────

def _run_mopup(job_id: str, req: MopupRequest, kb: KBConfig) -> None:
    try:
        result = mopup_run(
            kb=kb,
            score_threshold=req.score_threshold,
            word_threshold=req.word_threshold,
            max_stubs=req.max_stubs,
            run_defrag=req.run_defrag,
        )
        _jobs[job_id]["status"] = "done"
        _jobs[job_id]["result"] = result
    except Exception as exc:
        log.exception("mopup job %s failed", job_id)
        _jobs[job_id]["status"] = "error"
        _jobs[job_id]["error"]  = str(exc)


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/run")
async def start_mopup(
    req: MopupRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    """Start an async mop-up job. Returns job_id immediately."""
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "result": None, "error": None}
    background_tasks.add_task(_run_mopup, job_id, req, kb)
    log.info(
        "mopup job %s started  score_threshold=%d  word_threshold=%d  max_stubs=%d  defrag=%s",
        job_id, req.score_threshold, req.word_threshold, req.max_stubs, req.run_defrag,
    )
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_mopup_job(job_id: str):
    """Poll job status. status: running | done | error"""
    job = _jobs.get(job_id)
    if job is None:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


@router.post("/page/infer-subtype")
def infer_page_subtype(
    req: SubtypeRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """Run LLM subtype inference for one page and save the result."""
    path = kb.wiki_dir / req.page
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail=f"Page not found: {req.page}")

    import re
    title_m = re.search(r"^title:\s*(.+)$", content, re.MULTILINE)
    title = title_m.group(1).strip().strip("\"'") if title_m else req.page

    from ..services.quality_scorer import parse_scope
    scope = parse_scope(content)

    subtype = infer_subtype_llm(title, scope)
    set_page_subtype(req.page, subtype, kb)
    return {"page": req.page, "subtype": subtype}


@router.patch("/page/subtype")
def patch_page_subtype(
    req: SubtypeRequest,
    kb: KBConfig = Depends(resolve_kb),
):
    """Manually override the subtype frontmatter of a single page."""
    if req.subtype not in _VALID_SUBTYPES:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Unknown subtype {req.subtype!r}")
    set_page_subtype(req.page, req.subtype, kb)
    return {"page": req.page, "subtype": req.subtype}


@router.get("/queue")
def preview_queue(
    score_threshold: int = SCORE_THRESHOLD,
    word_threshold:  int = WORD_THRESHOLD,
    max_stubs:       int = MAX_STUBS,
    kb: KBConfig = Depends(resolve_kb),
):
    """
    Return the stub queue that would be processed — no LLM calls, no writes.
    Useful for reviewing candidates before committing to a run.
    """
    queue = get_stub_queue(kb.wiki_dir, score_threshold, word_threshold, max_stubs)
    return {
        "total": len(queue),
        "score_threshold": score_threshold,
        "word_threshold":  word_threshold,
        "pages": [
            {
                "page":    p["rel"],
                "title":   p["title"],
                "subtype": p["subtype"],
                "score":   p["score"],
                "cds":     p["cds"],
                "inbound": p["inbound"],
                "words":   p["words"],
            }
            for p in queue
        ],
    }
