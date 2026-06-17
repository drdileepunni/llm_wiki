"""
Summary-seeded multimodal transcription of treatment-chart / progress-note images.

A single Gemini multimodal structured-output call takes all chart images plus the
patient's running clinical narrative (so handwriting is read *in context* — e.g.
"this is the treatment chart of a CHF patient on furosemide and carvedilol") and
returns both the raw transcription and a structured medication list.

Returns the parsed dict plus the LLMUsage so the orchestrator can cost the step.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class ExtractedMed(BaseModel):
    name: str                       # drug name as written (generic if legible)
    dose: Optional[str] = None      # "40 mg", "0.1 mcg/kg/min"
    route: Optional[str] = None     # PO/IV/SC/...
    frequency: Optional[str] = None # "BD", "q6h", "OD" — as written
    status_on_chart: str            # active | held | stopped | unclear
    verbatim: str                   # exact handwriting transcription for this line
    confidence: str                 # high | medium | low


class TranscriptionResult(BaseModel):
    raw_text: str                   # full transcription across all pages
    extracted_meds: list[ExtractedMed]


_SYSTEM = (
    "You are a critical-care pharmacist transcribing handwritten ICU treatment charts and "
    "progress notes. Read carefully; use the provided patient context to disambiguate "
    "hard-to-read drug names, but NEVER invent medications that are not on the page. "
    "Transcribe verbatim first, then extract the medication list."
)


def _build_prompt(summary_narrative: str, n_images: int) -> str:
    ctx = summary_narrative.strip() or "(no running summary available)"
    return (
        f"PATIENT CONTEXT (for disambiguation only — do not treat as orders):\n{ctx}\n\n"
        f"You are given {n_images} image(s) of this patient's handwritten treatment chart / "
        "progress notes.\n\n"
        "1. Transcribe everything legible into `raw_text` (preserve structure: meds, doses, "
        "routes, frequencies, and any 'stopped'/'held'/'continue' annotations).\n"
        "2. Extract every MEDICATION into `extracted_meds`. For each: drug `name`, `dose`, "
        "`route`, `frequency` exactly as written; `status_on_chart` = active|held|stopped|unclear "
        "(use 'stopped'/'held' only when the chart explicitly marks it so, else 'active' or "
        "'unclear'); `verbatim` = the exact line you read it from; `confidence` = high|medium|low "
        "based on legibility.\n"
        "Only medications. Ignore labs, fluids charts, vitals, and non-drug notes."
    )


def _gemini_client(model: str | None):
    root = Path(__file__).resolve().parents[3]
    for p in (str(root / "app"), str(root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from backend.config import GOOGLE_API_KEY, MODEL
    from backend.services.llm_client import GeminiLLMClient
    return GeminiLLMClient(api_key=GOOGLE_API_KEY, model=model or MODEL)


def transcribe_treatment_charts(
    images: list[dict],
    summary_narrative: str,
    cpmrn: str,
    model: str | None = None,
) -> dict:
    """
    images: download_all output [{file_key, bytes, mime_type, error}].
    Returns {"raw_text", "extracted_meds": [dict], "usage": LLMUsage}.
    """
    from backend.services.llm_client import LLMUsage

    valid = [im for im in images if im.get("bytes")]
    if not valid:
        logger.warning("transcribe: no valid images for %s", cpmrn)
        return {"raw_text": "", "extracted_meds": [], "usage": LLMUsage(0, 0)}

    client = _gemini_client(model)
    prompt = _build_prompt(summary_narrative, len(valid))
    img_parts = [(im["bytes"], im.get("mime_type") or "image/jpeg") for im in valid]

    result, usage = client.generate_json_multimodal_with_usage(
        prompt=prompt,
        images=img_parts,
        schema=TranscriptionResult,
        system=_SYSTEM,
        max_tokens=4096,
    )
    return {
        "raw_text": result.get("raw_text", ""),
        "extracted_meds": result.get("extracted_meds", []) or [],
        "usage": usage,
    }
