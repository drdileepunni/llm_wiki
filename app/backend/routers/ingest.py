import asyncio
import logging
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Depends
import shutil
from ..services.extractor import extract_text, extract_pubmed, extract_url
from ..services.ingest_pipeline import run_ingest_chunked as run_ingest
from ..config import KBConfig
from ..dependencies import resolve_kb

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = logging.getLogger("wiki.ingest")

@router.post("/file")
async def ingest_file(file: UploadFile = File(...), kb: KBConfig = Depends(resolve_kb)):
    log.info("File ingest requested: %s  (content_type=%s)  kb=%s", file.filename, file.content_type, kb.name)

    dest = kb.raw_dir / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    log.info("Saved raw file → %s", dest)

    text = extract_text(dest, cache_dir=kb.cache_dir)
    log.info("Extracted %d chars from %s", len(text), file.filename)

    result = await asyncio.to_thread(
        run_ingest,
        text, file.filename, f"File: {file.filename}", str(dest), kb,
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result

@router.post("/pubmed")
async def ingest_pubmed(pmid: str = Form(...), kb: KBConfig = Depends(resolve_kb)):
    log.info("PubMed ingest requested: PMID=%s  kb=%s", pmid, kb.name)
    try:
        data = extract_pubmed(pmid)
        log.info("Fetched PubMed article: %r (%d chars)", data["title"], len(data["text"]))
    except Exception as e:
        log.error("Failed to fetch PMID %s: %s", pmid, e)
        raise HTTPException(status_code=400, detail=f"Failed to fetch PubMed ID {pmid}: {str(e)}")

    result = await asyncio.to_thread(
        run_ingest,
        data["text"], data["title"], data["citation"], f"pubmed:{pmid}", kb,
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result


@router.post("/url")
async def ingest_url(url: str = Form(...), kb: KBConfig = Depends(resolve_kb)):
    log.info("URL ingest requested: %s  kb=%s", url, kb.name)
    try:
        data = extract_url(url)
        log.info("Fetched URL: %r (%d chars)", data["title"], len(data["text"]))
    except Exception as e:
        log.error("Failed to fetch URL %s: %s", url, e)
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {str(e)}")

    result = await asyncio.to_thread(
        run_ingest,
        data["text"], data["title"], data["citation"], data["url"], kb,
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result
