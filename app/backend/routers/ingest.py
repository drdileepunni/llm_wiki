from fastapi import APIRouter, UploadFile, File, Form, HTTPException
from pathlib import Path
import shutil
from ..services.extractor import extract_text, extract_pubmed
from ..services.ingest_pipeline import run_ingest
from ..config import RAW_DIR

router = APIRouter(prefix="/api/ingest", tags=["ingest"])

@router.post("/file")
async def ingest_file(file: UploadFile = File(...)):
    # Save to raw/
    dest = RAW_DIR / file.filename
    with open(dest, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Extract text
    text = extract_text(dest)

    # Run pipeline
    result = run_ingest(
        source_text=text,
        source_name=file.filename,
        citation=f"File: {file.filename}"
    )

    return result

@router.post("/pubmed")
async def ingest_pubmed(pmid: str = Form(...)):
    try:
        data = extract_pubmed(pmid)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Failed to fetch PubMed ID {pmid}: {str(e)}")

    result = run_ingest(
        source_text=data["text"],
        source_name=data["title"],
        citation=data["citation"],
    )

    return result
