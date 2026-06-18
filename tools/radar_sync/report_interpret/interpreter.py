"""
Summary-seeded multimodal interpretation of a single diagnostic report.

A Gemini multimodal structured-output call takes the report image(s)/PDF plus the
patient's running clinical narrative (so the report is read *in context* — e.g. "this
echo belongs to a post-op patient with new respiratory distress") and returns:
  - a description (verbatim transcription for text/handwritten reports, or a visual
    description for raw images like an X-ray or ECG tracing),
  - a clinical interpretation in the patient's context,
  - a structured `findings` list — the channel that (in Phase 2) feeds the problem list.

Returns the parsed dict plus the LLMUsage so the orchestrator can cost the step.
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class Finding(BaseModel):
    label: str                       # concise clinical finding, e.g. "Congestive heart failure"
    is_new_diagnosis: bool           # True if this establishes a diagnosis not already evident
    body_system: str                 # cardiac | respiratory | neuro | renal | abdominal | msk | vascular | other
    severity: str                    # routine | notable | critical
    supporting_evidence: str         # what in the report supports it, e.g. "LVEF 30%, global hypokinesia"


class ReportInterpretation(BaseModel):
    report_type: str                 # normalized type: echo | ecg | xray | ct | mri | ultrasound | doppler | other
    description: str                 # verbatim transcription OR visual description of the image
    interpretation: str              # clinical reading in the patient's context
    findings: list[Finding]          # structured findings (may be empty for a normal report)
    confidence: str                  # high | medium | low


_SYSTEM = (
    "You are a senior intensivist and radiologist interpreting a diagnostic report for an "
    "ICU patient. First DESCRIBE what the report/image shows (transcribe text verbatim, or "
    "describe the image objectively). Then INTERPRET it in the patient's clinical context. "
    "Use the patient context only to disambiguate and contextualise — NEVER invent findings "
    "that are not supported by the report itself. If the report is normal, say so and return "
    "an empty findings list."
)


def _build_prompt(report_name: str, summary_narrative: str, n_images: int) -> str:
    ctx = (summary_narrative or "").strip() or "(no running summary available)"
    return (
        f"PATIENT CONTEXT (for disambiguation/contextualisation only — not a substitute for "
        f"the report):\n{ctx}\n\n"
        f"REPORT: {report_name or 'diagnostic report'} — you are given {n_images} image(s)/page(s).\n\n"
        "1. `report_type`: normalize to one of echo | ecg | xray | ct | mri | ultrasound | "
        "doppler | other.\n"
        "2. `description`: transcribe all legible text verbatim; for an image without text "
        "(e.g. X-ray, ECG tracing), give an objective visual description (structures, "
        "abnormalities, measurements visible).\n"
        "3. `interpretation`: a concise clinical reading in THIS patient's context — what does "
        "it mean for their current problems and management.\n"
        "4. `findings`: list each distinct clinical finding. For each: `label` (concise dx/"
        "finding), `is_new_diagnosis` (true only if it establishes something not already "
        "obvious from context), `body_system`, `severity` (routine|notable|critical), "
        "`supporting_evidence` (the specific report detail). Return an EMPTY list if the "
        "report is normal / unremarkable.\n"
        "5. `confidence`: high|medium|low based on image legibility and diagnostic clarity."
    )


def _gemini_client(model: str | None):
    root = Path(__file__).resolve().parents[3]
    for p in (str(root / "app"), str(root)):
        if p not in sys.path:
            sys.path.insert(0, p)
    from backend.config import GOOGLE_API_KEY, MODEL
    from backend.services.llm_client import GeminiLLMClient
    return GeminiLLMClient(api_key=GOOGLE_API_KEY, model=model or MODEL)


def interpret_report(
    images: list[dict],
    report_name: str,
    summary_narrative: str,
    cpmrn: str,
    model: str | None = None,
) -> dict:
    """
    images: download_all output [{file_key, bytes, mime_type, error}] for ONE report
            (one entry for a single image, multiple for a multi-page PDF/report).
    Returns {"result": ReportInterpretation-as-dict, "usage": LLMUsage}.
    """
    from backend.services.llm_client import LLMUsage

    valid = [im for im in images if im.get("bytes")]
    if not valid:
        logger.warning("interpret_report: no valid images for %s (%s)", cpmrn, report_name)
        empty = {"report_type": "other", "description": "", "interpretation": "",
                 "findings": [], "confidence": "low"}
        return {"result": empty, "usage": LLMUsage(0, 0)}

    client = _gemini_client(model)
    prompt = _build_prompt(report_name, summary_narrative, len(valid))
    img_parts = [(im["bytes"], im.get("mime_type") or "image/jpeg") for im in valid]

    result, usage = client.generate_json_multimodal_with_usage(
        prompt=prompt,
        images=img_parts,
        schema=ReportInterpretation,
        system=_SYSTEM,
        max_tokens=4096,
    )
    return {
        "result": {
            "report_type": result.get("report_type", "other"),
            "description": result.get("description", ""),
            "interpretation": result.get("interpretation", ""),
            "findings": result.get("findings", []) or [],
            "confidence": result.get("confidence", "medium"),
        },
        "usage": usage,
    }
