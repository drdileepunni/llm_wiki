import logging
from fastapi import APIRouter
from pydantic import BaseModel
from ..services.chat_pipeline import run_chat, file_answer

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = logging.getLogger("wiki.chat")

class ChatRequest(BaseModel):
    question: str

class FileRequest(BaseModel):
    question: str
    answer: str

@router.post("/")
async def chat(req: ChatRequest):
    log.info("Chat question: %r", req.question[:120])
    return run_chat(req.question)

@router.post("/file")
async def file_query(req: FileRequest):
    log.info("Filing answer for question: %r", req.question[:80])
    filename = file_answer(req.question, req.answer)
    log.info("Filed → %s", filename)
    return {"filed": True, "filename": filename}
