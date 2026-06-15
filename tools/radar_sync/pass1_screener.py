"""
Pass 1 screener — cheap gemini-3.1-flash-lite call (no thinking) that:
  1. Decides if full Pass 2 (status_classifier + problem_tracker) is needed
  2. Produces a lightweight clinical summary stored in patient_contexts
     regardless of whether Pass 2 runs

Called by scheduler.py after summary_updater, before status_classifier.
On failure, defaults to needs_full_analysis=True so safety is preserved.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_SCREENER_MODEL = "gemini-3.1-flash-lite"

# Re-use the same ABG allowlist as summary_updater so the screener never flags
# Na, K, glucose, ionized Ca drawn from point-of-care blood gas machines.
_BLOOD_GAS_PANEL_KW = ("gas panel", "abg", "vbg", "arterial blood gas",
                        "venous blood gas", "blood gas")
_BLOOD_GAS_KEEP_ATTRS = ("ph", "pao2", "paco2", "bicarb", "hco3",
                          "lactic", "lactate", "be", "base excess", "cso2",
                          "spo2", "fio2")


def _filter_screener_lab_attrs(lab: dict) -> list[tuple[str, Any]]:
    """
    Return (key, value) pairs from a lab document's attributes,
    applying the same blood-gas allowlist as summary_updater._filter_lab_attrs.
    Gas panel attributes not in the allowlist (Na, K, glucose, iCa…) are dropped.
    """
    name = (lab.get("name") or "").lower()
    attrs = lab.get("attributes") or {}
    valued = [
        (k, v.get("value") if isinstance(v, dict) else v)
        for k, v in attrs.items()
        if (v.get("value") if isinstance(v, dict) else v) not in (None, "")
    ]
    is_gas = any(kw in name for kw in _BLOOD_GAS_PANEL_KW)
    if not is_gas:
        return valued
    return [(k, v) for k, v in valued if any(keep in k.lower() for keep in _BLOOD_GAS_KEEP_ATTRS)]


@dataclass
class Pass1Result:
    needs_full_analysis: bool
    next_run_hours: int       # 1, 2, or 4
    flag_reason: str          # empty string if not flagging
    lightweight_summary: dict


# ── System prompts ─────────────────────────────────────────────────────────────

_SYSTEM_STANDARD = """\
You are an ICU triage assistant. Review the patient summary and recent new data.
Decide if a full clinical re-analysis (with deep reasoning) is needed right now.

Flag needs_full_analysis=true if ANY of:
- Any problem newly worsening or critical compared to prior state
- A vital or lab crossed a danger threshold: SpO2 <92%, K+ >5.5 mmol/L,
  lactate >2 mmol/L, Cr rising >20% vs prior, Hb <7 g/dL, MAP <60 mmHg
- A new clinical note documents a new problem, a plan change, or treatment failure
- A worsening/critical problem has no documented management plan

next_run_hours: 1 if flagging, 2 if borderline worth watching, 4 if all stable with plans.
Flag needs_full_analysis=true ONLY for clear threshold crossings or significant new problems.\
"""

_SYSTEM_HIGH_SENSITIVITY = """\
You are an ICU triage assistant. Review the patient summary and recent new data.
Decide if a full clinical re-analysis (with deep reasoning) is needed right now.

IMPORTANT: This patient had a WORSENING or CRITICAL problem in the last full analysis.
Apply HEIGHTENED sensitivity — flag for full analysis even on borderline changes.

Flag needs_full_analysis=true if ANY of:
- Any problem newly worsening or critical compared to prior state
- A vital or lab crossed a danger threshold: SpO2 <92%, K+ >5.5 mmol/L,
  lactate >2 mmol/L, Cr rising >20% vs prior, Hb <7 g/dL, MAP <60 mmHg
- A new clinical note documents a new problem, a plan change, or treatment failure
- A worsening/critical problem has no documented management plan
- ANY new lab or vital for a problem that was worsening/critical in the last run
  (even borderline — re-verify with full reasoning)

next_run_hours: default 1 for this patient; use 2 only if clearly improving; 4 only if all resolved.\
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
            "next_run_hours": {
                "type": "integer",
                "description": "1, 2, or 4 — hours until next screener run",
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
        "required": ["needs_full_analysis", "next_run_hours", "flag_reason",
                     "overall_trajectory", "problems"],
    },
}


# ── Main entry point ───────────────────────────────────────────────────────────

def screen_patient(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    delta: dict,
    last_problems: list[dict],
    db: Any,
) -> Pass1Result:
    """
    Run the Pass 1 screener for one patient.

    Args:
        structured_summary: output from summary_updater (PatientSummary dict)
        delta:              output from extract_delta (new_vitals, new_labs, new_notes, ...)
        last_problems:      list of patient_problems docs from MongoDB
        db:                 MongoDB database handle

    Returns Pass1Result. Never raises — on failure defaults to needs_full_analysis=True.
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

    # Select sensitivity mode based on prior problem states
    has_prior_worsening = any(
        p.get("clinical_status") in ("worsening", "critical") for p in last_problems
    )
    system = _SYSTEM_HIGH_SENSITIVITY if has_prior_worsening else _SYSTEM_STANDARD

    # Build compact delta — strip huge IO data, keep essentials
    delta_compact = {
        "new_vitals_count": len(delta.get("new_vitals") or []),
        "new_labs_count":   len(delta.get("new_labs") or []),
        "new_notes_count":  len(delta.get("new_notes") or []),
        "new_vitals": [
            {k: v for k, v in vit.items()
             if k in ("timestamp","daysHR","daysBP","daysMAP","daysSpO2",
                      "daysRR","daysFiO2","daysTemp")}
            for vit in (delta.get("new_vitals") or [])[:3]
        ],
        "new_labs": [
            {
                "name":       lab.get("name", ""),
                "reportedAt": str(lab.get("reportedAt", "")),
                "values": {
                    k: (v.get("value") if isinstance(v, dict) else v)
                    for k, v in _filter_screener_lab_attrs(lab)
                },
            }
            for lab in (delta.get("new_labs") or [])[:5]
            if _filter_screener_lab_attrs(lab)  # skip panels that become empty after filtering
        ],
        "new_notes": [
            {
                "timestamp": str(n.get("timestamp", "")),
                "note_type": n.get("note_type", ""),
                "preview":   (n.get("text", "") or "")[:200],
            }
            for n in (delta.get("new_notes") or [])[:3]
        ],
    }

    prior_compact = [
        {
            "problem_name":    p.get("problem_name", ""),
            "clinical_status": p.get("clinical_status", ""),
            "being_addressed": p.get("being_addressed", False),
        }
        for p in last_problems
    ]

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n\n"
        f"Current problems from PatientSummary:\n"
        f"{json.dumps(structured_summary.get('problems', []), indent=2, default=str)}\n\n"
        f"Prior full-analysis state:\n"
        f"{json.dumps(prior_compact, indent=2, default=str)}\n\n"
        f"New data since last analysis:\n"
        f"{json.dumps(delta_compact, indent=2, default=str)}\n\n"
        f"Call submit_triage_result with your assessment."
    )

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_SCREENER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="pass1_screener", db=db)
    tracer.start_round(0)

    try:
        resp = client.create_message(
            messages=[{"role": "user", "content": user_msg}],
            tools=[_SUBMIT_TOOL],
            system=system,
            max_tokens=1024,
            force_tool=True,
            thinking_budget=None,   # no thinking — this is the cheap screener
        )
        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens)
    except Exception:
        logger.exception("pass1_screener: LLM call failed for %s enc=%d", cpmrn, encounter)
        tracer.end_round()
        tracer.save(final_output={"error": "llm_failed"})
        return _fallback(structured_summary, "pass1 LLM call failed — defaulting to full analysis")

    tracer.end_round()

    result_data: dict = {}
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_triage_result":
            result_data = block.input
            break

    if not result_data:
        logger.warning("pass1_screener: no tool result for %s enc=%d", cpmrn, encounter)
        tracer.save(final_output={"error": "no_tool_result"})
        return _fallback(structured_summary, "pass1 returned no result — defaulting to full analysis")

    needs_full = bool(result_data.get("needs_full_analysis", True))
    next_run_h = int(result_data.get("next_run_hours", 1 if needs_full else 4))
    if next_run_h not in (1, 2, 4):
        next_run_h = 1 if needs_full else 4

    flag_reason = result_data.get("flag_reason", "")

    lightweight_summary = {
        "computed_at":        datetime.now(timezone.utc),
        "overall_trajectory": result_data.get("overall_trajectory", "stable"),
        "problems":           result_data.get("problems", []),
        "pass1_flag":         needs_full,
        "pass1_reason":       flag_reason,
        "sensitivity_mode":   "high" if has_prior_worsening else "standard",
    }

    tracer.save(final_output={
        "needs_full_analysis": needs_full,
        "next_run_hours":      next_run_h,
        "flag_reason":         flag_reason,
        "overall_trajectory":  lightweight_summary["overall_trajectory"],
    })

    logger.info(
        "pass1_screener: %s enc=%d → needs_full=%s next=%dh reason=%s",
        cpmrn, encounter, needs_full, next_run_h, flag_reason or "(none)",
    )

    return Pass1Result(
        needs_full_analysis=needs_full,
        next_run_hours=next_run_h,
        flag_reason=flag_reason,
        lightweight_summary=lightweight_summary,
    )


def _fallback(structured_summary: dict, reason: str) -> Pass1Result:
    """Safe fallback when screener fails — always request full analysis."""
    return Pass1Result(
        needs_full_analysis=True,
        next_run_hours=1,
        flag_reason=reason,
        lightweight_summary={
            "computed_at":        datetime.now(timezone.utc),
            "overall_trajectory": "unknown",
            "problems": [
                {
                    "name":            p.get("name", ""),
                    "status":          p.get("status", ""),
                    "key_change":      "",
                    "management_note": "",
                }
                for p in structured_summary.get("problems", [])
            ],
            "pass1_flag":       True,
            "pass1_reason":     reason,
            "sensitivity_mode": "high",
        },
    )
