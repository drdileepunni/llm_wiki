"""
Log capture endpoints.

POST /api/logs/capture/start   — start buffering filtered logs to memory
POST /api/logs/capture/stop    — stop + write to file, return filename
GET  /api/logs/capture/status  — current state (active, line count, last file)
GET  /api/logs/files           — list saved capture files
GET  /api/logs/download/{fn}   — download a saved capture file
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..services.log_capture import _LOG_DIR, capture_handler

log = logging.getLogger("wiki.logs")

router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.post("/capture/start")
def start_capture():
    capture_handler.start()
    log.info("Log capture started")
    return {"status": "capturing", "started_at": capture_handler.started_at}


@router.post("/capture/stop")
def stop_capture():
    if not capture_handler.active:
        raise HTTPException(status_code=400, detail="No capture in progress")
    path = capture_handler.stop()
    filename = Path(path).name
    log.info("Log capture stopped — %d lines → %s", capture_handler.last_line_count, filename)
    return {
        "status": "saved",
        "filename": filename,
        "path": path,
        "lines": capture_handler.last_line_count,
    }


@router.get("/capture/status")
def capture_status():
    return {
        "active": capture_handler.active,
        "started_at": capture_handler.started_at,
        "lines": capture_handler.line_count,
        "last_file": capture_handler.last_file,
    }


@router.get("/files")
def list_files():
    return {"files": capture_handler.list_files()}


@router.get("/download/{filename}")
def download_file(filename: str):
    # Prevent path traversal — only serve files from _LOG_DIR
    safe = (_LOG_DIR / filename).resolve()
    if not str(safe).startswith(str(_LOG_DIR.resolve())):
        raise HTTPException(status_code=400, detail="Invalid filename")
    if not safe.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(safe, filename=filename, media_type="text/plain")
