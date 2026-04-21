"""
Endpoints for resolving wiki knowledge gaps via PubMed search.

POST /api/resolve/search   — find relevant articles (blocking ~20-30s)
POST /api/resolve/ingest   — start async ingest job, returns job_id immediately
GET  /api/resolve/jobs/{id} — poll job status
"""

import asyncio
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.gap_resolver import fetch_article_content, search_for_gap
from ..services.ingest_pipeline import run_ingest_chunked

router = APIRouter(prefix="/api/resolve", tags=["resolve"])

# In-memory job store — sufficient for a single-process dev server
_jobs: dict[str, dict] = {}


# ── Request models ────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    gap_title:   str
    gap_sections: list[str]
    max_results: int = 5


class IngestRequest(BaseModel):
    pmc_id:   str
    title:    str
    citation: str = ""


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.post("/search")
async def resolve_search(req: SearchRequest, kb: KBConfig = Depends(resolve_kb)):
    """Search Google/PubMed for articles that would fill the gap. Takes ~20-30s."""
    articles = await asyncio.to_thread(
        search_for_gap, req.gap_title, req.gap_sections, req.max_results
    )
    return {"articles": articles}


@router.post("/ingest")
async def resolve_ingest(
    req: IngestRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    """Start an async ingest job for a PMC article. Returns job_id immediately."""
    job_id = str(uuid.uuid4())[:8]
    _jobs[job_id] = {"status": "running", "title": req.title, "result": None, "error": None}
    background_tasks.add_task(_do_ingest, job_id, req.pmc_id, req.title, req.citation, kb)
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


# ── Background task ───────────────────────────────────────────────────────────

async def _do_ingest(job_id: str, pmc_id: str, title: str, citation: str, kb: KBConfig):
    try:
        text = await asyncio.to_thread(fetch_article_content, pmc_id)
        if not text:
            _jobs[job_id] = {
                "status": "error", "title": title, "result": None,
                "error": "Could not fetch article content from PMC",
            }
            return

        if not citation:
            citation = f"PubMed Central. {title}. PMC{pmc_id}."

        result = await asyncio.to_thread(run_ingest_chunked, text, title, citation, "", kb)
        _jobs[job_id] = {"status": "done", "title": title, "result": result, "error": None}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "title": title, "result": None, "error": str(exc)}
