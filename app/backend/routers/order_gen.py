from fastapi import APIRouter, Depends
from pydantic import BaseModel
from ..services.order_gen_pipeline import run_order_generation
from ..dependencies import resolve_kb
from ..config import KBConfig

router = APIRouter(prefix="/api/orders", tags=["orders"])


class GenerateRequest(BaseModel):
    recommendations: list[str]
    cpmrn: str | None = None
    patient_type: str = "adult"
    model: str | None = None


@router.post("/generate")
def generate_orders(req: GenerateRequest, kb: KBConfig = Depends(resolve_kb)):
    return run_order_generation(
        recommendations=req.recommendations,
        cpmrn=req.cpmrn,
        patient_type=req.patient_type,
        model=req.model,
        kb=kb,
    )
