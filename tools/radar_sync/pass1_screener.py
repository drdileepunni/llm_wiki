"""
Pass 1 screener — cheap gemini-3.1-flash-lite call (no thinking) that:
  1. Decides if full Pass 2 (status_classifier + problem_tracker) is needed
  2. Produces a lightweight clinical summary stored in patient_contexts
     regardless of whether Pass 2 runs

Called by scheduler.py after summary_updater, before status_classifier.
On failure, defaults to needs_full_analysis=False (skip Pass 2) so a
screener crash doesn't burn the expensive model.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_SCREENER_MODEL = "gemini-3.1-flash-lite"


@dataclass
class Pass1Result:
    needs_full_analysis: bool
    flag_reason: str          # empty string if not flagging
    lightweight_summary: dict


# ── System prompt ──────────────────────────────────────────────────────────────

_SYSTEM = """\
You are an ICU clinical note screener. A new clinical note has arrived for this
patient. Read it and decide if it contains anything that warrants a full
clinical re-analysis with deep reasoning.

Flag needs_full_analysis=true if the note documents ANY of:
- A new clinical problem not previously tracked
- Treatment failure or inadequate response to current management
- A plan change that suggests clinical deterioration
- Any finding the treating team considers significant or worrying

Do NOT flag:
- Routine nursing handover or observation notes with no new clinical content
- Notes that only confirm stable, ongoing management is continuing as planned
- Administrative, discharge planning, or documentation-only entries
- A note that describes the patient as currently stable or improved, even if the stored
  problem status is "worsening" or "critical" — the stored status reflects a historical
  trend established over prior assessments; a single note capturing the last hour does
  not contradict it. Flag only if the note itself contains NEW deterioration, NEW
  abnormal findings, or a NEW clinical problem.\
"""


# ── Tool schema ────────────────────────────────────────────────────────────────

_SUBMIT_TOOL = {
    "name": "submit_triage_result",
    "description": "Submit the triage decision after reviewing the patient data.",
    "input_schema": {
        "type": "object",
        "properties": {
            "needs_full_analysis": {
                "type": "boolean",
                "description": "True if full Pass 2 deep-reasoning analysis is needed now",
            },
            "flag_reason": {
                "type": "string",
                "description": "One sentence explaining why full analysis is needed, or empty string",
            },
            "overall_trajectory": {
                "type": "string",
                "enum": ["stable", "improving", "worsening", "critical"],
            },
            "problems": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name":            {"type": "string"},
                        "status":          {"type": "string",
                                            "enum": ["stable","improving","worsening","critical","resolved"]},
                        "key_change":      {"type": "string",
                                            "description": "Key change since last run, or empty string"},
                        "management_note": {"type": "string",
                                            "description": "What team is doing, or empty string"},
                    },
                    "required": ["name", "status", "key_change", "management_note"],
                },
            },
        },
        "required": ["needs_full_analysis", "flag_reason", "overall_trajectory", "problems"],
    },
}


# ── Main entry point ───────────────────────────────────────────────────────────

def screen_patient(
    cpmrn: str,
    encounter: int,
    delta: dict,
    last_problems: list[dict],
    db: Any,
) -> Pass1Result:
    """
    Run the Pass 1 screener for one patient.

    Args:
        delta:         output from extract_delta (new_vitals, new_labs, new_notes, ...)
        last_problems: list of patient_problems docs from MongoDB
        db:            MongoDB database handle

    Returns Pass1Result. Never raises — on failure defaults to needs_full_analysis=False.
    """
    import sys, json
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from tools.radar_sync.react_tracer import ReActTracer

    # Notes with full text (up to 1000 chars). Vitals and labs are handled by
    # deterministic gates upstream — the screener only evaluates note content.
    new_notes = [
        {
            "timestamp": str(n.get("timestamp", "")),
            "note_type": n.get("note_type", ""),
            "text":      (n.get("text", "") or "")[:1000],
        }
        for n in (delta.get("new_notes") or [])[:5]
    ]

    # Known problems — for context and lightweight summary output only.
    known_problems = [
        {"name": p.get("problem_name", ""), "status": p.get("clinical_status", "")}
        for p in last_problems
    ]

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n\n"
        f"Known problems (for summary context only — do NOT flag based on whether the note "
        f"matches these statuses; statuses reflect historical trend, not the current hour):\n"
        f"{json.dumps(known_problems, indent=2, default=str)}\n\n"
        f"New note(s) to evaluate:\n"
        f"{json.dumps(new_notes, indent=2, default=str)}\n\n"
        f"Call submit_triage_result with your assessment."
    )

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_SCREENER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="pass1_screener", db=db)
    tracer.start_round(0)

    try:
        resp = client.create_message(
            messages=[{"role": "user", "content": user_msg}],
            tools=[_SUBMIT_TOOL],
            system=_SYSTEM,
            max_tokens=1024,
            force_tool=True,
            thinking_budget=None,   # no thinking — this is the cheap screener
        )
        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        logger.exception("pass1_screener: LLM call failed for %s enc=%d", cpmrn, encounter)
        tracer.end_round()
        tracer.save(final_output={"error": "llm_failed"})
        return _fallback(last_problems, "pass1 LLM call failed — defaulting to skip")

    tracer.end_round()

    result_data: dict = {}
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_triage_result":
            result_data = block.input
            break

    if not result_data:
        logger.warning("pass1_screener: no tool result for %s enc=%d", cpmrn, encounter)
        tracer.save(final_output={"error": "no_tool_result"})
        return _fallback(last_problems, "pass1 returned no result — defaulting to skip")

    needs_full  = bool(result_data.get("needs_full_analysis", False))
    flag_reason = result_data.get("flag_reason", "")

    lightweight_summary = {
        "computed_at":        datetime.now(timezone.utc),
        "overall_trajectory": result_data.get("overall_trajectory", "stable"),
        "problems":           result_data.get("problems", []),
        "pass1_flag":         needs_full,
        "pass1_reason":       flag_reason,
    }

    tracer.save(final_output={
        "needs_full_analysis": needs_full,
        "flag_reason":         flag_reason,
        "overall_trajectory":  lightweight_summary["overall_trajectory"],
    })

    logger.info(
        "pass1_screener: %s enc=%d → needs_full=%s reason=%s",
        cpmrn, encounter, needs_full, flag_reason or "(none)",
    )

    return Pass1Result(
        needs_full_analysis=needs_full,
        flag_reason=flag_reason,
        lightweight_summary=lightweight_summary,
    )


def _fallback(last_problems: list[dict], reason: str) -> Pass1Result:
    """Fallback when screener fails — skip Pass 2 rather than burning the expensive model."""
    return Pass1Result(
        needs_full_analysis=False,
        flag_reason=reason,
        lightweight_summary={
            "computed_at":        datetime.now(timezone.utc),
            "overall_trajectory": "unknown",
            "problems": [
                {
                    "name":            p.get("problem_name", ""),
                    "status":          p.get("clinical_status", ""),
                    "key_change":      "",
                    "management_note": "",
                }
                for p in last_problems
            ],
            "pass1_flag":   False,
            "pass1_reason": reason,
        },
    )
