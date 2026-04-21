import asyncio
import logging
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from ..services.chat_pipeline import run_chat, file_answer
from ..config import KBConfig
from ..dependencies import resolve_kb

router = APIRouter(prefix="/api/chat", tags=["chat"])
log = logging.getLogger("wiki.chat")

class ChatRequest(BaseModel):
    question: str

class FileRequest(BaseModel):
    question: str
    answer: str

@router.post("/")
async def chat(req: ChatRequest, kb: KBConfig = Depends(resolve_kb)):
    log.info("Chat question: %r  kb=%s", req.question[:120], kb.name)
    return await asyncio.to_thread(run_chat, req.question, kb)

@router.post("/file")
async def file_query(req: FileRequest, kb: KBConfig = Depends(resolve_kb)):
    log.info("Filing answer for question: %r  kb=%s", req.question[:80], kb.name)
    filename = await asyncio.to_thread(file_answer, req.question, req.answer, kb)
    log.info("Filed → %s", filename)
    return {"filed": True, "filename": filename}
