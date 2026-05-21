"""
Status classifier — reasoning model that verifies worsening/critical labels
by examining vital and lab trends across stored snapshots before the CDS gate fires.

Flow:
  1. summary_updater assigns draft statuses (single-snapshot, no trajectory context)
  2. classify_statuses() runs a reasoning model for each worsening/critical problem
  3. Model calls get_vital_trend / get_lab_trend / query_patient_notes to check trajectory
  4. Model calls set_problem_statuses once satisfied — final statuses replace the draft
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_CLASSIFIER_MODEL = "gemini-2.5-flash"  # reasoning model with thinking
_MAX_TOOL_ROUNDS  = 8                   # max tool calls before forcing final answer
_THINKING_BUDGET  = 8000               # tokens for Gemini thinking

_SYSTEM = """You are a senior ICU clinician reviewing automatically-assigned clinical problem statuses.

A prior model labelled each problem from a single snapshot. Your job is to VERIFY these labels by \
checking TREND DATA — not just the latest value. You have tools to pull vital and lab trends, \
and to search clinical notes.

STATUS CRITERIA (apply strictly):
• critical  — immediate life threat, RIGHT NOW:
    Vasopressors running AND haemodynamically unstable (MAP <60 despite treatment),
    SpO2 <88% on FiO2 >60% with no improvement trend,
    Serum K >6.5 mmol/L (not correcting), pH <7.1, active uncontrolled haemorrhage,
    GCS acutely falling, refractory arrhythmia.
• worsening — measurable deterioration across ≥2 consecutive readings:
    e.g. HR 90→105→118 over 3 readings, creatinine 1.0→1.5→2.1 across panels,
    SpO2 trending down on the SAME or increasing FiO2.
    A single abnormal reading is NOT worsening. A chronic stable abnormal is NOT worsening.
• stable    — within acceptable target range, OR single abnormal reading without trend,
    OR known chronic baseline not changing.
• improving — measurable recovery across ≥2 readings.
• resolved  — problem no longer active.

RULES:
- Always check at least vital trend OR lab trend before finalising worsening/critical.
- A problem already on appropriate treatment with controlled values → stable, not worsening.
- Chronic hypertension at 150/90 with no recent change → stable.
- Tachycardia HR 105 if prior 4 readings were all 100-110 → stable (known baseline).
- Call set_problem_statuses once — after you have gathered enough evidence."""


# ── Tool schemas (Anthropic format — GeminiLLMClient converts automatically) ──

_VITAL_TREND_TOOL = {
    "name": "get_vital_trend",
    "description": (
        "Get the last N vital-sign readings for this patient from stored snapshots. "
        "Use to assess trajectory rather than just the latest value. "
        "Returns readings newest-first with timestamps."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "vital_name": {
                "type": "string",
                "description": "One of: HR, BP, MAP, SpO2, RR, FiO2",
            },
            "n": {
                "type": "integer",
                "description": "Number of readings to return (default 8, max 20)",
            },
        },
        "required": ["vital_name"],
    },
}

_LAB_TREND_TOOL = {
    "name": "get_lab_trend",
    "description": (
        "Get recent values for a specific lab parameter across stored snapshots. "
        "Use to check if a value is rising, falling, or stable. "
        "Search by partial name — e.g. 'Hb', 'Creatinine', 'Potassium', 'Glucose', 'Lactate'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "lab_name": {
                "type": "string",
                "description": "Lab parameter name (partial match OK): Hb, Creatinine, Potassium, Glucose, Lactate, Platelets, WBC, Sodium, Bilirubin, pH, PaO2, PaCO2",
            },
            "n": {
                "type": "integer",
                "description": "Number of recent values to return (default 6, max 12)",
            },
        },
        "required": ["lab_name"],
    },
}

_NOTES_TOOL = {
    "name": "query_patient_notes",
    "description": (
        "Semantic search over all clinician notes for this patient. "
        "Use to find clinical context about a problem's course, recent events, or treatment decisions."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "Clinical question, e.g. 'tachycardia trend and management', 'renal function course', 'hemodynamic status'",
            },
        },
        "required": ["question"],
    },
}

_SET_STATUS_TOOL = {
    "name": "set_problem_statuses",
    "description": (
        "After reviewing trend evidence, set the final verified status for each problem. "
        "Call this ONCE when you are satisfied with the evidence gathered."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "problem_name": {
                            "type": "string",
                            "description": "Exact problem name as given",
                        },
                        "status": {
                            "type": "string",
                            "enum": ["critical", "worsening", "stable", "improving", "resolved"],
                        },
                        "reasoning": {
                            "type": "string",
                            "description": "One sentence: what trend data supported this status",
                        },
                    },
                    "required": ["problem_name", "status", "reasoning"],
                },
            },
        },
        "required": ["assessments"],
    },
}

_TOOLS = [_VITAL_TREND_TOOL, _LAB_TREND_TOOL, _NOTES_TOOL, _SET_STATUS_TOOL]

# ── Vital field map ────────────────────────────────────────────────────────────

_VITAL_FIELD = {
    "HR":   "daysHR",
    "BP":   "daysBP",
    "MAP":  "daysMAP",
    "SPO2": "daysSpO2",
    "RR":   "daysRR",
    "FIO2": "daysFiO2",
    "TEMP": "daysTemp",
}


# ── Tool implementations ───────────────────────────────────────────────────────

def _get_vital_trend(cpmrn: str, encounter: int, vital_name: str, n: int = 8) -> str:
    from backend.services.emr.db import get_db
    from tools.radar_sync.summary_updater import _to_ist

    n = min(max(n, 1), 20)
    field = _VITAL_FIELD.get(vital_name.upper().replace("SPO2", "SPO2").replace("FIO2", "FIO2"))
    if not field:
        return f"Unknown vital '{vital_name}'. Use one of: HR, BP, MAP, SpO2, RR, FiO2"

    db = get_db()
    snaps = list(db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.vitals": 1, "snapshot_at": 1},
    ).sort("snapshot_at", -1).limit(n * 3))  # fetch more, may deduplicate

    seen_ts = set()
    rows = []
    for snap in snaps:
        vitals = (snap.get("chart") or {}).get("vitals") or []
        for v in vitals[:3]:  # newest readings in each snapshot
            ts_raw = v.get("timestamp")
            val = v.get(field)
            if val is None or ts_raw in seen_ts:
                continue
            seen_ts.add(ts_raw)
            rows.append((ts_raw, val))
            if len(rows) >= n:
                break
        if len(rows) >= n:
            break

    if not rows:
        return f"No {vital_name} readings found in stored snapshots."

    lines = [f"{vital_name} trend (newest first):"]
    for ts_raw, val in rows:
        lines.append(f"  [{_to_ist(ts_raw)}] {val}")
    return "\n".join(lines)


def _get_lab_trend(cpmrn: str, encounter: int, lab_name: str, n: int = 6) -> str:
    from backend.services.emr.db import get_db
    from tools.radar_sync.summary_updater import _to_ist

    n = min(max(n, 1), 12)
    db = get_db()
    snaps = list(db.snapshots.find(
        {"CPMRN": cpmrn, "encounter": encounter},
        {"chart.documents": 1, "snapshot_at": 1},
    ).sort("snapshot_at", -1).limit(n * 4))

    search = lab_name.lower()
    # Lab attribute aliases for common short names
    _ATTR_ALIASES = {
        "hb": ["hb", "hemoglobin", "haemoglobin"],
        "k":  ["potassium", "k"],
        "na": ["sodium", "na"],
        "cr": ["creatinine", "cr"],
        "wbc": ["total count", "wbc", "white blood cell"],
        "plt": ["platelets", "plt"],
    }
    aliases = _ATTR_ALIASES.get(search, [search])

    seen_ts = set()
    rows = []
    for snap in snaps:
        docs = [(snap.get("chart") or {}).get("documents") or []]
        for doc_list in docs:
            for doc in doc_list:
                if doc.get("category") != "labs":
                    continue
                ts_raw = doc.get("reportedAt")
                if ts_raw in seen_ts:
                    continue
                # Search panel name
                panel_name = (doc.get("name") or "").lower()
                attrs = doc.get("attributes") or {}
                # Look for matching attribute key
                for attr_key, attr_val in attrs.items():
                    if not isinstance(attr_val, dict):
                        continue
                    ak_lower = attr_key.lower()
                    if any(al in ak_lower for al in aliases):
                        val = attr_val.get("value")
                        unit = attr_val.get("unit", "")
                        if val is not None:
                            seen_ts.add(ts_raw)
                            rows.append((ts_raw, attr_key, val, unit))
                            break
        if len(rows) >= n:
            break

    if not rows:
        return f"No '{lab_name}' values found in stored snapshots."

    rows_sorted = sorted(rows, key=lambda r: str(r[0]), reverse=True)[:n]
    lines = [f"{lab_name} trend (newest first):"]
    for ts_raw, attr_key, val, unit in rows_sorted:
        lines.append(f"  [{_to_ist(ts_raw)}] {attr_key} = {val} {unit}".rstrip())
    return "\n".join(lines)


def _run_tool(name: str, args: dict, cpmrn: str, encounter: int) -> str:
    try:
        if name == "get_vital_trend":
            return _get_vital_trend(cpmrn, encounter, args["vital_name"], int(args.get("n", 8)))
        if name == "get_lab_trend":
            return _get_lab_trend(cpmrn, encounter, args["lab_name"], int(args.get("n", 6)))
        if name == "query_patient_notes":
            from tools.radar_sync.query_notes import query_patient_notes
            return query_patient_notes(cpmrn, encounter, args["question"])
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception("status_classifier: tool %s failed", name)
        return f"Tool error: {e}"


# ── Main entry point ───────────────────────────────────────────────────────────

def classify_statuses(cpmrn: str, encounter: int, structured_summary: dict) -> dict:
    """
    Run a reasoning model over the draft PatientSummary.
    Only problems currently labelled worsening/critical are reviewed.
    Returns an updated structured_summary with verified statuses.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    problems: list[dict] = structured_summary.get("problems", [])
    candidates = [p for p in problems if p.get("status") in ("worsening", "critical")]

    if not candidates:
        logger.info("status_classifier: no worsening/critical problems to review for %s", cpmrn)
        return structured_summary

    logger.info(
        "status_classifier: reviewing %d problem(s) for %s — %s",
        len(candidates), cpmrn, [p["name"] for p in candidates],
    )

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from backend.services.emr.db import get_db as _get_db
    from tools.radar_sync.react_tracer import ReActTracer

    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_CLASSIFIER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="status_classifier", db=_get_db())

    # Build the initial user message
    problem_block = "\n".join(
        f"  {i+1}. {p['name']} [{p['status'].upper()}]\n"
        f"     Current state: {p.get('current_state', 'not specified')}\n"
        f"     Management: {p.get('management', 'not specified')}"
        for i, p in enumerate(candidates)
    )

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n\n"
        f"The following problems were preliminarily labelled WORSENING or CRITICAL "
        f"from a single chart snapshot. Please verify each by checking trend data.\n\n"
        f"{problem_block}\n\n"
        f"Use get_vital_trend and get_lab_trend to check trajectories, then call "
        f"set_problem_statuses with your verified assessments."
    )

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    final_assessments: list[dict] = []

    for round_num in range(_MAX_TOOL_ROUNDS):
        tracer.start_round(round_num)
        try:
            force = round_num < _MAX_TOOL_ROUNDS - 1  # last round: don't force tool
            resp = client.create_message(
                messages=messages,
                tools=_TOOLS,
                system=_SYSTEM,
                max_tokens=4096,
                force_tool=force,
                thinking_budget=_THINKING_BUDGET,
            )
        except Exception:
            logger.exception("status_classifier: LLM call failed on round %d for %s", round_num, cpmrn)
            tracer.end_round()
            break

        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens)

        # Collect assistant content for history
        assistant_parts: list[dict] = []
        tool_calls: list[dict] = []

        for block in resp.content:
            if block.type == "thinking":
                tracer.log_thinking(block.text)
                # thinking blocks are not added to message history
            elif block.type == "tool_use":
                tool_calls.append(block)
                tracer.log_tool_call(block.name, block.input)
                part: dict = {
                    "type": "tool_use",
                    "name": block.name,
                    "input": block.input,
                    "id": block.id,
                }
                if block.thought_signature:
                    part["thought_signature"] = block.thought_signature
                assistant_parts.append(part)
            else:
                tracer.log_text(block.text)
                assistant_parts.append({"type": "text", "text": block.text})

        if assistant_parts:
            messages.append({"role": "assistant", "content": assistant_parts})

        tracer.end_round()

        if not tool_calls:
            break

        # Execute tools
        tool_results: list[dict] = []
        for tc in tool_calls:
            if tc.name == "set_problem_statuses":
                final_assessments = tc.input.get("assessments", [])
                logger.info(
                    "status_classifier: final statuses for %s: %s",
                    cpmrn,
                    [(a["problem_name"], a["status"]) for a in final_assessments],
                )
                tool_results.append({
                    "type": "tool_result",
                    "name": tc.name,
                    "id": tc.id,
                    "content": "Status assessments recorded.",
                })
            else:
                result_text = _run_tool(tc.name, tc.input, cpmrn, encounter)
                tracer.log_tool_result(tc.name, result_text)
                logger.info("status_classifier: tool %s → %d chars", tc.name, len(result_text))
                tool_results.append({
                    "type": "tool_result",
                    "name": tc.name,
                    "id": tc.id,
                    "content": result_text,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        # Stop once set_problem_statuses was called
        if final_assessments:
            break

    # Apply verified statuses back to the summary
    if not final_assessments:
        logger.warning("status_classifier: no assessments returned for %s — keeping draft statuses", cpmrn)
        tracer.save(final_output={"error": "no_assessments"})
        return structured_summary

    name_to_assessment = {a["problem_name"]: a for a in final_assessments}
    updated_problems = []
    for p in problems:
        assessment = name_to_assessment.get(p["name"])
        if assessment:
            old_status = p["status"]
            new_status = assessment["status"]
            if old_status != new_status:
                logger.info(
                    "status_classifier: %s '%s' %s → %s (%s)",
                    cpmrn, p["name"], old_status, new_status, assessment.get("reasoning", ""),
                )
            p = {**p, "status": new_status, "classifier_reasoning": assessment.get("reasoning", "")}
        updated_problems.append(p)

    result = {**structured_summary, "problems": updated_problems}
    tracer.save(final_output={
        "assessments": [(a["problem_name"], a["status"]) for a in final_assessments],
    })
    return result
