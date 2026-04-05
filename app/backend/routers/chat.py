from fastapi import APIRouter
from pydantic import BaseModel
from ..services.chat_pipeline import run_chat, file_answer

router = APIRouter(prefix="/api/chat", tags=["chat"])

class ChatRequest(BaseModel):
    question: str

class FileRequest(BaseModel):
    question: str
    answer: str

@router.post("/")
async def chat(req: ChatRequest):
    return run_chat(req.question)

@router.post("/file")
async def file_query(req: FileRequest):
    filename = file_answer(req.question, req.answer)
    return {"filed": True, "filename": filename}
