"""
Endpoints for resolving wiki knowledge gaps via PubMed search.

POST /api/resolve/search        — find relevant articles (blocking ~20-30s)
POST /api/resolve/ingest        — start async ingest job, returns job_id immediately
GET  /api/resolve/jobs/{id}     — poll job status
POST /api/resolve/gap-from-query — create a gap file from an unanswered query
"""

import asyncio
import logging
import uuid
from datetime import date
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel

from ..config import KBConfig
from ..dependencies import resolve_kb
from ..services.fill_sections_pipeline import fill_sections
from ..services.gap_resolver import fetch_article_content, search_for_gap, BBPool, _llm_fallback
from ..services.llm_client import get_llm_client

log = logging.getLogger("wiki.resolve")

router = APIRouter(prefix="/api/resolve", tags=["resolve"])

# In-memory job store — sufficient for a single-process dev server
_jobs: dict[str, dict] = {}
_batches: dict[str, dict] = {}


# ── Request models ────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    gap_title:   str
    gap_sections: list[str]
    max_results: int = 5


class IngestRequest(BaseModel):
    pmc_id:           str
    title:            str
    citation:         str = ""
    gap_title:        str = ""
    gap_sections:     list[str] = []
    gap_file:         str = ""   # e.g. "wiki/gaps/saline.md"
    referenced_page:  str = ""   # e.g. "wiki/entities/saline.md"


class GapFromQueryRequest(BaseModel):
    question: str
    answer:   str


class ResolveAllRequest(BaseModel):
    max_results: int = 3


class VerifyGapsRequest(BaseModel):
    gap_stem: str = ""   # empty = verify all non-patient gaps


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.delete("/gaps/{gap_stem}")
def delete_gap(gap_stem: str, kb: KBConfig = Depends(resolve_kb)):
    """Delete a gap file by its stem (filename without .md)."""
    from ..services import vector_store as vs_mod
    gap_path = kb.wiki_dir / "gaps" / f"{gap_stem}.md"
    if not gap_path.exists():
        raise HTTPException(status_code=404, detail=f"Gap file not found: {gap_stem}")
    vs_mod.remove(f"gaps/{gap_stem}.md", kb.wiki_dir)
    gap_path.unlink()
    return {"deleted": gap_stem}


@router.post("/search")
async def resolve_search(req: SearchRequest, kb: KBConfig = Depends(resolve_kb)):
    """Search Google/PubMed for articles that would fill the gap. Takes ~20-30s."""
    articles, _ = await asyncio.to_thread(
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
    background_tasks.add_task(
        _do_ingest,
        job_id, req.pmc_id, req.title, req.citation,
        req.gap_title, req.gap_sections, req.gap_file, req.referenced_page,
        kb,
    )
    return {"job_id": job_id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        return {"job_id": job_id, "status": "not_found", "result": None, "error": None}
    return {"job_id": job_id, **job}


@router.post("/resolve-all")
async def resolve_all(
    req: ResolveAllRequest,
    background_tasks: BackgroundTasks,
    kb: KBConfig = Depends(resolve_kb),
):
    """Start async resolution of all pending gaps. Returns batch_id immediately."""
    batch_id = str(uuid.uuid4())[:8]
    _batches[batch_id] = {"status": "running", "total_gaps": 0, "completed_gaps": 0, "job_ids": []}
    background_tasks.add_task(_do_resolve_all, batch_id, req.max_results, kb)
    return {"batch_id": batch_id}


@router.get("/batch/{batch_id}")
def get_batch(batch_id: str):
    batch = _batches.get(batch_id)
    if not batch:
        return {"batch_id": batch_id, "status": "not_found", "total_gaps": 0, "completed_gaps": 0, "jobs": []}
    jobs = [
        {"job_id": jid, "title": _jobs.get(jid, {}).get("title", ""), "status": _jobs.get(jid, {}).get("status", "running")}
        for jid in batch.get("job_ids", [])
    ]
    return {
        "batch_id": batch_id,
        "status": batch["status"],
        "total_gaps": batch["total_gaps"],
        "completed_gaps": batch["completed_gaps"],
        "jobs": jobs,
    }


# ── Background tasks ──────────────────────────────────────────────────────────

async def _do_resolve_all(batch_id: str, max_results: int, kb: KBConfig):
    gaps_dir = kb.wiki_dir / "gaps"
    gaps = []
    if gaps_dir.exists():
        for f in sorted(gaps_dir.glob("*.md")):
            text = f.read_text(encoding="utf-8", errors="replace")
            referenced_page = ""
            subtype = ""
            section_times_opened: dict = {}
            for line in text.splitlines():
                if line.startswith("referenced_page:"):
                    referenced_page = line.split(":", 1)[1].strip()
                elif line.startswith("subtype:"):
                    subtype = line.split(":", 1)[1].strip()
                elif line.startswith("section_times_opened:"):
                    import json as _json
                    try:
                        section_times_opened = _json.loads(line.split(":", 1)[1].strip())
                    except Exception:
                        section_times_opened = {}

            # Merge with persistent history so resolved+reopened gaps still escalate
            try:
                from ..services.ingest_pipeline import _load_gap_history
                history = _load_gap_history(gaps_dir)
                for s, cnt in history.get(f.stem, {}).items():
                    section_times_opened[s] = max(section_times_opened.get(s, 0), cnt)
            except Exception:
                pass
            missing: list[str] = []
            missing_values: list[str] = []
            resolution_question = ""
            in_missing = False
            in_question = False
            in_missing_values = False
            for line in text.splitlines():
                if line.strip() == "## Missing Sections":
                    in_missing = True; in_question = False; in_missing_values = False
                    continue
                if line.strip() == "## Resolution Question":
                    in_question = True; in_missing = False; in_missing_values = False
                    continue
                if line.strip() == "## Specific Missing Values":
                    in_missing_values = True; in_missing = False; in_question = False
                    continue
                if line.startswith("##"):
                    in_missing = False; in_question = False; in_missing_values = False
                    continue
                if in_missing and line.startswith("- "):
                    item = line[2:].strip()
                    if not item.startswith("RESOLVED:"):
                        missing.append(item)
                if in_question and line.strip() and not resolution_question:
                    resolution_question = line.strip()
                if in_missing_values and line.startswith("- "):
                    missing_values.append(line[2:].strip())
            if missing and not f.stem.startswith("patient-"):
                # Re-route referenced_page through canonical registry so stale
                # gap files pointing at narrow/renamed pages get consolidated.
                from ..services.canonical_registry import resolve as _resolve_canonical
                gap_title = f.stem.replace("-", " ").title()
                canonical_page = _resolve_canonical(gap_title, kb.wiki_dir)
                effective_page = canonical_page or referenced_page
                log.info(
                    "resolve-all: gap=%r  sections=%s  missing_values=%s  question=%s",
                    gap_title, missing, missing_values,
                    resolution_question[:100] if resolution_question else "none",
                )
                gaps.append({
                    "file": f"wiki/gaps/{f.name}",
                    "title": gap_title,
                    "referenced_page": effective_page,
                    "subtype": subtype,
                    "missing_sections": missing,
                    "resolution_question": resolution_question,
                    "missing_values": missing_values,
                    "section_times_opened": section_times_opened,
                })

    _batches[batch_id]["total_gaps"] = len(gaps)
    log.info("resolve-all batch %s: %d gaps", batch_id, len(gaps))

    # Pre-create 3 shared BB sessions for the whole batch (reduces billed minutes
    # from N×60s to just 3×actual_duration).
    def _start_pool():
        p = BBPool(size=3)
        p.start()
        return p

    pool = await asyncio.to_thread(_start_pool)

    async def _ingest_article(article, gap, skip_quality_gate=None):
        job_id = str(uuid.uuid4())[:8]
        _jobs[job_id] = {"status": "running", "title": article.get("title", ""), "result": None, "error": None}
        _batches[batch_id]["job_ids"].append(job_id)
        gate = skip_quality_gate if skip_quality_gate is not None else bool(article.get("skip_quality_gate"))
        await _do_ingest(
            job_id,
            article.get("pmc_id", ""),
            article.get("title", ""),
            article.get("citation", ""),
            gap["title"],
            gap["missing_sections"],
            gap["file"],
            gap["referenced_page"],
            kb,
            prefetched_content=article.get("content", ""),
            skip_quality_gate=gate,
            hint_subtype=gap.get("subtype", ""),
            missing_values=gap.get("missing_values") or [],
        )
        result = _jobs[job_id].get("result") or {}
        return len(result.get("files_written", []))

    _SECTION_LLM_THRESHOLD = 2  # go straight to LLM if any section has been filed this many times

    async def process_gap(gap):
        try:
            section_times = gap.get("section_times_opened") or {}
            escalated_sections = [
                s for s in gap["missing_sections"]
                if section_times.get(s, 0) >= _SECTION_LLM_THRESHOLD
            ]
            use_llm_directly = bool(escalated_sections)

            log.info(
                "process_gap: title=%r  sections=%s  missing_values=%s  question=%s  escalated=%s",
                gap["title"], gap["missing_sections"],
                gap.get("missing_values") or [],
                (gap.get("resolution_question") or "")[:100],
                escalated_sections or "none",
            )

            files_written = 0

            if use_llm_directly:
                log.info(
                    "resolve-all gap '%s': sections %s filed %d+ times — skipping PubMed, using LLM directly",
                    gap["title"], escalated_sections, _SECTION_LLM_THRESHOLD,
                )
                llm_articles, _ = await asyncio.to_thread(
                    _llm_fallback,
                    gap["title"],
                    gap["missing_sections"],
                    gap.get("resolution_question", ""),
                    gap.get("missing_values") or [],
                )
                for article in llm_articles:
                    files_written += await _ingest_article(article, gap, skip_quality_gate=True)
            else:
                articles, _ = await asyncio.to_thread(
                    search_for_gap,
                    gap["title"],
                    gap["missing_sections"],
                    max_results,
                    pool,
                    gap.get("resolution_question", ""),
                    gap.get("missing_values") or [],
                )
                for article in articles:
                    files_written += await _ingest_article(article, gap)

                # LLM fallback if PubMed articles were found but nothing got written.
                if files_written == 0:
                    log.info("resolve-all gap '%s': 0 files written — triggering LLM fallback", gap["title"])
                    llm_articles, _ = await asyncio.to_thread(
                        _llm_fallback,
                        gap["title"],
                        gap["missing_sections"],
                        gap.get("resolution_question", ""),
                        gap.get("missing_values") or [],
                    )
                    for article in llm_articles:
                        await _ingest_article(article, gap, skip_quality_gate=True)
        except Exception as exc:
            log.warning("resolve-all gap '%s' search failed: %s", gap["title"], exc)
        finally:
            _batches[batch_id]["completed_gaps"] += 1

    try:
        await asyncio.gather(*[process_gap(g) for g in gaps])
    finally:
        await asyncio.to_thread(pool.stop)

    _batches[batch_id]["status"] = "done"
    log.info("resolve-all batch %s done: %d jobs started", batch_id, len(_batches[batch_id]["job_ids"]))


_GAP_EXTRACT_TOOL = {
    "name": "create_knowledge_gap",
    "description": "Extract the knowledge gap implied by an unanswered wiki query",
    "input_schema": {
        "type": "object",
        "required": ["entity_title", "page_path", "missing_sections"],
        "properties": {
            "entity_title": {
                "type": "string",
                "description": "Primary entity or concept the question is about (e.g. 'Severe Bradycardia', 'Atropine')",
            },
            "page_path": {
                "type": "string",
                "description": "Relative wiki path for the target page, e.g. wiki/entities/severe-bradycardia.md or wiki/concepts/emergency-pacing.md",
            },
            "missing_sections": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific section headings that would answer the question (2-5 items)",
            },
            "subtype": {
                "type": "string",
                "enum": ["medication", "investigation", "procedure", "condition", "default"],
                "description": "Page structural type of the target entity page.",
            },
        },
    },
}


@router.post("/verify-gaps")
async def verify_gaps(req: VerifyGapsRequest, kb: KBConfig = Depends(resolve_kb)):
    """
    Verify whether gap queries are now answerable by the vector store.
    Pass gap_stem to check one gap, or omit to check all non-patient gaps.
    Returns list of {gap_title, gap_file, referenced_page, queries} per gap.
    """
    from ..services.fill_sections_pipeline import _verify_gap_closure
    gaps_dir = kb.wiki_dir / "gaps"
    if not gaps_dir.exists():
        return {"results": []}

    if req.gap_stem:
        gap_files = [gaps_dir / f"{req.gap_stem}.md"]
    else:
        gap_files = sorted(f for f in gaps_dir.glob("*.md") if not f.stem.startswith("patient-"))

    def _run_verify():
        results = []
        for gf in gap_files:
            if not gf.exists():
                continue
            text = gf.read_text(encoding="utf-8", errors="replace")

            referenced_page = ""
            for line in text.splitlines():
                if line.startswith("referenced_page:"):
                    referenced_page = line.split(":", 1)[1].strip()
                    break

            missing: list[str] = []
            missing_values: list[str] = []
            resolved: list[str] = []
            in_missing = in_mv = False
            for line in text.splitlines():
                s = line.strip()
                if s == "## Missing Sections":
                    in_missing = True; in_mv = False
                elif s == "## Specific Missing Values":
                    in_mv = True; in_missing = False
                elif s.startswith("## "):
                    in_missing = in_mv = False
                elif in_missing and line.startswith("- "):
                    item = line[2:].strip()
                    if item.startswith("RESOLVED:"):
                        resolved.append(item[len("RESOLVED:"):].strip())
                    else:
                        missing.append(item)
                elif in_mv and line.startswith("- "):
                    missing_values.append(line[2:].strip())

            sections = missing or resolved
            if not sections:
                continue

            target_path = referenced_page if referenced_page.startswith("wiki/") else f"wiki/{referenced_page}" if referenced_page else f"wiki/entities/{gf.stem}.md"
            full_path = kb.wiki_root / target_path
            page_content = full_path.read_text(encoding="utf-8") if full_path.exists() else ""

            queries = _verify_gap_closure(
                target_path, page_content, sections, missing_values or None, kb.wiki_dir,
            )
            results.append({
                "gap_title": gf.stem.replace("-", " ").title(),
                "gap_file":  f"wiki/gaps/{gf.name}",
                "gap_stem":  gf.stem,
                "referenced_page": referenced_page,
                "page_exists": full_path.exists(),
                "has_pending": bool(missing),
                "queries": queries,
            })
        return results

    results = await asyncio.to_thread(_run_verify)
    return {"results": results}


@router.post("/gap-from-query")
async def gap_from_query(req: GapFromQueryRequest, kb: KBConfig = Depends(resolve_kb)):
    """Create or update a gap file from a query the wiki couldn't answer."""
    result = await asyncio.to_thread(_extract_and_write_gap, req.question, req.answer, kb)
    return result


def _extract_and_write_gap(question: str, answer: str, kb: KBConfig) -> dict:
    client = get_llm_client()
    prompt = (
        f"A user asked the following question and received an incomplete answer from a wiki. "
        f"Extract the primary knowledge gap so it can be tracked for future ingestion.\n\n"
        f"Question: {question}\n\nAnswer received: {answer}"
    )
    resp = client.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[_GAP_EXTRACT_TOOL],
        system="You extract knowledge gaps from unanswered wiki queries. Be concise and specific.",
        max_tokens=512,
        force_tool=True,
    )
    tool_block = next((b for b in resp.content if b.type == "tool_use"), None)
    if not tool_block:
        raise ValueError("LLM did not return a tool call")

    inp = tool_block.input
    entity_title: str = inp["entity_title"]
    missing: list[str] = inp.get("missing_sections", [])
    subtype: str = inp.get("subtype", "default") or "default"

    if not missing:
        raise ValueError("LLM returned no missing sections")

    # Remap to canonical page so repeated gaps for the same concept consolidate.
    from ..services.canonical_registry import resolve as _resolve_canonical
    page_path = _resolve_canonical(entity_title, kb.wiki_dir)
    stem = Path(page_path).stem
    gaps_dir = kb.wiki_dir / "gaps"
    gaps_dir.mkdir(parents=True, exist_ok=True)
    gap_path = gaps_dir / f"{stem}.md"
    today = date.today().isoformat()
    created = today

    existing: set[str] = set()
    if gap_path.exists():
        text = gap_path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith("created:"):
                created = line.split(":", 1)[1].strip().strip('"')
            if line.startswith("- ") and "## Missing" not in text[:text.find(line)]:
                pass
        # reuse ingest_pipeline helper via inline parse
        in_block = False
        for line in text.splitlines():
            if line.strip() == "## Missing Sections":
                in_block = True
                continue
            if in_block:
                if line.startswith("##"):
                    break
                if line.startswith("- "):
                    existing.add(line[2:].strip())

    merged = sorted(existing | set(missing))
    gap_rel = f"wiki/gaps/{stem}.md"
    subtype_line = f"subtype: {subtype}\n" if subtype else ""
    content = (
        f"---\n"
        f'title: "Knowledge Gap \u2014 {entity_title}"\n'
        f"type: gap\n"
        f"{subtype_line}"
        f"referenced_page: {page_path}\n"
        f"tags: [gap]\n"
        f"created: {created}\n"
        f"updated: {today}\n"
        f"---\n\n"
        f"## Missing Sections\n\n"
        + "\n".join(f"- {s}" for s in merged)
        + f"\n\n## Suggested Sources\n\n"
        f"- Ingest a reference document, guideline, or monograph that covers the above sections for: **{entity_title}**\n"
        f"\n_Flagged by unanswered query: {question[:120]}_\n"
    )
    gap_path.write_text(content, encoding="utf-8")
    log.info("gap-from-query: wrote %s (%d sections)", gap_rel, len(merged))

    return {
        "gap_file": gap_rel,
        "entity_title": entity_title,
        "page_path": page_path,
        "missing_sections": merged,
    }


async def _do_ingest(
    job_id: str,
    pmc_id: str,
    title: str,
    citation: str,
    gap_title: str,
    gap_sections: list[str],
    gap_file: str,
    referenced_page: str,
    kb: KBConfig,
    prefetched_content: str = "",
    skip_quality_gate: bool = False,
    hint_subtype: str = "",
    missing_values: list[str] | None = None,
):
    log.info(
        "_do_ingest: gap=%r  sections=%s  missing_values=%s  page=%s  skip_quality_gate=%s",
        gap_title, gap_sections, missing_values or [], referenced_page, skip_quality_gate,
    )
    try:
        if prefetched_content:
            text = prefetched_content
        else:
            text = await asyncio.to_thread(fetch_article_content, pmc_id)
        if not text:
            _jobs[job_id] = {
                "status": "error", "title": title, "result": None,
                "error": "Could not fetch article content from PMC",
            }
            return

        if not citation:
            citation = f"PubMed Central. {title}. PMC{pmc_id}."

        result = await asyncio.to_thread(
            fill_sections,
            text, title, citation,
            gap_title or title,
            gap_sections,
            referenced_page,
            gap_file,
            kb,
            skip_quality_gate,
            hint_subtype,
            missing_values or [],
        )
        _jobs[job_id] = {"status": "done", "title": title, "result": result, "error": None}

    except Exception as exc:
        _jobs[job_id] = {"status": "error", "title": title, "result": None, "error": str(exc)}
