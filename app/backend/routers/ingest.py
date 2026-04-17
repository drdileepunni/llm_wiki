import logging
from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pathlib import Path
import shutil
from ..services.extractor import extract_text, extract_pubmed, extract_url
from ..services.ingest_pipeline import run_ingest_chunked as run_ingest
from ..config import RAW_DIR

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
log = logging.getLogger("wiki.ingest")

@router.post("/file")
async def ingest_file(file: UploadFile = File(...)):
    log.info("File ingest requested: %s  (content_type=%s)", file.filename, file.content_type)

    # Save to raw/
    dest = RAW_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)
    log.info("Saved raw file → %s", dest)

    # Extract text
    text = extract_text(dest)
    log.info("Extracted %d chars from %s", len(text), file.filename)

    # Run pipeline
    result = run_ingest(
        source_text=text,
        source_name=file.filename,
        citation=f"File: {file.filename}",
        source_path=str(dest),
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result

@router.post("/pubmed")
async def ingest_pubmed(pmid: str = Form(...)):
    log.info("PubMed ingest requested: PMID=%s", pmid)
    try:
        data = extract_pubmed(pmid)
        log.info("Fetched PubMed article: %r (%d chars)", data["title"], len(data["text"]))
    except Exception as e:
        log.error("Failed to fetch PMID %s: %s", pmid, e)
        raise HTTPException(status_code=400, detail=f"Failed to fetch PubMed ID {pmid}: {str(e)}")

    result = run_ingest(
        source_text=data["text"],
        source_name=data["title"],
        citation=data["citation"],
        source_path=f"pubmed:{pmid}",
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result


@router.post("/url")
async def ingest_url(url: str = Form(...)):
    log.info("URL ingest requested: %s", url)
    try:
        data = extract_url(url)
        log.info("Fetched URL: %r (%d chars)", data["title"], len(data["text"]))
    except Exception as e:
        log.error("Failed to fetch URL %s: %s", url, e)
        raise HTTPException(status_code=400, detail=f"Failed to fetch URL: {str(e)}")

    result = run_ingest(
        source_text=data["text"],
        source_name=data["title"],
        citation=data["citation"],
        source_path=data["url"],
    )

    log.info(
        "Ingest complete: files_written=%d  errors=%d  cost=$%.4f",
        len(result.get("files_written", [])),
        len(result.get("errors", [])),
        result.get("cost_usd", 0),
    )
    return result
