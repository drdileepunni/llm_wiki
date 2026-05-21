"""
Problem tracker — per-patient ReAct reasoning loop that maintains a persistent
problem list in MongoDB and fires targeted alerts.

Flow (runs once per patient per hourly scheduler cycle, replacing the CDS gate):
  1. Load current structured_summary.problems[] from summary_updater output
  2. Load stored patient_problems[] from MongoDB
  3. Run a single ReAct reasoning session (gemini-2.5-flash with thinking)
     — model has tools to query vitals, labs, notes, and stored problem state
     — model calls set_all_assessments() once when done
  4. Upsert patient_problems[] in MongoDB
  5. Fire Google Chat alert for any problem with should_alert=True

Alert rules (applied by code, not model):
  - New problem, not being addressed → alert immediately
  - Existing problem: next_check overdue (due_after elapsed) AND not fulfilled → alert
  - Never re-alert the same problem within ALERT_COOLDOWN_HOURS if still being addressed

next_check timing (hardcoded):
  - type "vital" → due_after = now + 1h
  - type "lab"   → due_after = now + 6h
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any

logger = logging.getLogger(__name__)

_TRACKER_MODEL    = "gemini-2.5-flash"
_MAX_TOOL_ROUNDS  = 10
_THINKING_BUDGET  = 8000
_ALERT_COOLDOWN_H = 8   # minimum hours between repeat alerts for same problem

_NEXT_CHECK_HOURS = {"vital": 1, "lab": 6}

_SYSTEM = """You are a senior ICU clinician reviewing the current problem list for a patient.

You will receive:
1. The current structured_summary with problems and their clinical statuses
2. The stored problem state from the last assessment (if any)

For EACH problem, you must determine:
1. Is the problem being addressed? Query notes to find documentation of a plan or treatment.
   "Being addressed" means a documented plan exists — not necessarily that it is working yet.
2. What is the next thing to check? Set a next_check with:
   - "what": specific thing to look for (e.g. "Hb post-transfusion", "HR on Cardizem")
   - "type": "vital" or "lab"
   (The system will compute due_after automatically: vital=1h, lab=6h)
3. Should we alert? Alert ONLY if:
   - Problem is new AND NOT being addressed (no documented plan)
   - A pending next_check is now OVERDUE and the expected result is NOT present in the chart

IMPORTANT RULES:
- "Being addressed" = documented plan exists, even if result not yet visible (e.g. transfusion ongoing)
- Do NOT alert just because a problem is worsening/critical and has a plan — trust the plan
- Do NOT alert if the next_check is not yet overdue
- For each overdue next_check: use get_vital_trend or get_lab_trend to check if the result arrived
- If treatment is documented but the problem is worsening DESPITE adequate time for response:
  set being_addressed=False, should_alert=True, alert_reason="Treatment inadequate — [details]"
- Call set_all_assessments ONCE after reviewing all problems."""


# ── Tool definitions ──────────────────────────────────────────────────────────

_VITAL_TREND_TOOL = {
    "name": "get_vital_trend",
    "description": "Get the last N vital-sign readings from stored snapshots. Returns newest-first.",
    "input_schema": {
        "type": "object",
        "properties": {
            "vital_name": {"type": "string", "description": "HR, BP, MAP, SpO2, RR, FiO2"},
            "n": {"type": "integer", "description": "Number of readings (default 6, max 20)"},
        },
        "required": ["vital_name"],
    },
}

_LAB_TREND_TOOL = {
    "name": "get_lab_trend",
    "description": "Get recent values for a lab parameter across stored snapshots. Partial name match OK.",
    "input_schema": {
        "type": "object",
        "properties": {
            "lab_name": {"type": "string", "description": "e.g. Hb, Creatinine, Potassium, Glucose, Lactate"},
            "n": {"type": "integer", "description": "Number of values (default 6, max 12)"},
        },
        "required": ["lab_name"],
    },
}

_NOTES_TOOL = {
    "name": "query_patient_notes",
    "description": "Semantic search over all clinician notes. Use to find documented plans and treatments.",
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "e.g. 'anemia transfusion plan', 'Afib rate control'"},
        },
        "required": ["question"],
    },
}

_GET_PROBLEM_TOOL = {
    "name": "get_problem_state",
    "description": (
        "Fetch the stored state for a specific problem from the last assessment. "
        "Returns: being_addressed, addressed_evidence, next_check (what/type/due_after), last_alerted_at."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "problem_name": {"type": "string"},
        },
        "required": ["problem_name"],
    },
}

_SET_ALL_TOOL = {
    "name": "set_all_assessments",
    "description": "Call ONCE after reviewing all problems. Provide the full assessment for every problem.",
    "input_schema": {
        "type": "object",
        "properties": {
            "assessments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "problem_name":       {"type": "string"},
                        "clinical_status":    {
                            "type": "string",
                            "enum": ["critical", "worsening", "stable", "improving", "resolved"],
                        },
                        "being_addressed":    {"type": "boolean"},
                        "addressed_evidence": {"type": "string", "description": "Quote from notes or 'No plan documented'"},
                        "should_alert":       {"type": "boolean"},
                        "alert_reason":       {"type": "string", "description": "Required if should_alert=True"},
                        "suggestions":        {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific actionable suggestions if alerting",
                        },
                        "next_check": {
                            "type": "object",
                            "properties": {
                                "what": {"type": "string"},
                                "type": {"type": "string", "enum": ["vital", "lab"]},
                            },
                            "required": ["what", "type"],
                        },
                    },
                    "required": [
                        "problem_name", "clinical_status", "being_addressed",
                        "addressed_evidence", "should_alert", "next_check",
                    ],
                },
            },
        },
        "required": ["assessments"],
    },
}

_TOOLS = [_VITAL_TREND_TOOL, _LAB_TREND_TOOL, _NOTES_TOOL, _GET_PROBLEM_TOOL, _SET_ALL_TOOL]


# ── Tool implementations ───────────────────────────────────────────────────────

def _get_problem_state(cpmrn: str, encounter: int, problem_name: str, db: Any) -> str:
    doc = db["patient_problems"].find_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        {"being_addressed": 1, "addressed_evidence": 1, "next_check": 1, "last_alerted_at": 1},
    )
    if not doc:
        return f"No prior state for '{problem_name}' — this is a new problem."

    nc = doc.get("next_check") or {}
    due = nc.get("due_after")
    due_str = due.strftime("%Y-%m-%d %H:%M UTC") if isinstance(due, datetime) else str(due or "not set")
    overdue = isinstance(due, datetime) and datetime.now(timezone.utc) > due

    last_alert = doc.get("last_alerted_at")
    last_alert_str = last_alert.strftime("%Y-%m-%d %H:%M UTC") if isinstance(last_alert, datetime) else "never"

    return (
        f"Problem: {problem_name}\n"
        f"  Being addressed: {doc.get('being_addressed', False)}\n"
        f"  Evidence: {doc.get('addressed_evidence', 'none')}\n"
        f"  Next check: {nc.get('what', 'none')} ({nc.get('type', '?')}) — due {due_str}"
        f"{' [OVERDUE]' if overdue else ''}\n"
        f"  Last alerted: {last_alert_str}"
    )


def _run_tool(name: str, args: dict, cpmrn: str, encounter: int, db: Any) -> str:
    try:
        if name == "get_vital_trend":
            from tools.radar_sync.status_classifier import _get_vital_trend
            return _get_vital_trend(cpmrn, encounter, args["vital_name"], int(args.get("n", 6)))
        if name == "get_lab_trend":
            from tools.radar_sync.status_classifier import _get_lab_trend
            return _get_lab_trend(cpmrn, encounter, args["lab_name"], int(args.get("n", 6)))
        if name == "query_patient_notes":
            from tools.radar_sync.query_notes import query_patient_notes
            return query_patient_notes(cpmrn, encounter, args["question"])
        if name == "get_problem_state":
            return _get_problem_state(cpmrn, encounter, args["problem_name"], db)
        return f"Unknown tool: {name}"
    except Exception as e:
        logger.exception("problem_tracker: tool %s failed", name)
        return f"Tool error: {e}"


# ── Alert eligibility check ────────────────────────────────────────────────────

def _should_suppress_alert(cpmrn: str, encounter: int, problem_name: str, db: Any) -> bool:
    """Return True if we alerted recently and should hold off."""
    doc = db["patient_problems"].find_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        {"last_alerted_at": 1},
    )
    if not doc:
        return False
    last = doc.get("last_alerted_at")
    if not isinstance(last, datetime):
        return False
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - last) < timedelta(hours=_ALERT_COOLDOWN_H)


# ── Upsert problem state ───────────────────────────────────────────────────────

def _upsert_problem(
    cpmrn: str,
    encounter: int,
    assessment: dict,
    now: datetime,
    alerted: bool,
    db: Any,
) -> None:
    problem_name = assessment["problem_name"]
    nc_raw = assessment.get("next_check") or {}
    nc_type = nc_raw.get("type", "vital")
    due_after = now + timedelta(hours=_NEXT_CHECK_HOURS.get(nc_type, 1))

    next_check = {
        "what":      nc_raw.get("what", ""),
        "type":      nc_type,
        "due_after": due_after,
    }

    audit_entry = {
        "assessed_at":        now,
        "clinical_status":    assessment.get("clinical_status"),
        "being_addressed":    assessment.get("being_addressed"),
        "addressed_evidence": assessment.get("addressed_evidence", ""),
        "next_check":         next_check,
        "alerted":            alerted,
    }

    update: dict = {
        "$set": {
            "CPMRN":              cpmrn,
            "encounter":          encounter,
            "problem_name":       problem_name,
            "clinical_status":    assessment.get("clinical_status"),
            "being_addressed":    assessment.get("being_addressed"),
            "addressed_evidence": assessment.get("addressed_evidence", ""),
            "next_check":         next_check,
            "last_assessed_at":   now,
        },
        "$setOnInsert": {"first_detected_at": now},
        "$push": {"assessments": {"$each": [audit_entry], "$slice": -50}},  # keep last 50
    }

    if alerted:
        update["$set"]["last_alerted_at"] = now

    db["patient_problems"].update_one(
        {"CPMRN": cpmrn, "encounter": encounter, "problem_name": problem_name},
        update,
        upsert=True,
    )


# ── Main entry point ───────────────────────────────────────────────────────────

def track_problems(
    cpmrn: str,
    encounter: int,
    structured_summary: dict,
    snapshot_at: datetime,
) -> dict:
    """
    Run the problem tracker ReAct loop for one patient.
    Returns a status dict for the scheduler result entry.
    """
    import sys
    from pathlib import Path
    _root = Path(__file__).resolve().parents[2]
    for _p in [str(_root / "app"), str(_root)]:
        if _p not in sys.path:
            sys.path.insert(0, _p)

    from dotenv import load_dotenv
    load_dotenv(_root / "app" / ".env")

    from backend.config import GOOGLE_API_KEY
    from backend.services.llm_client import GeminiLLMClient
    from backend.services.emr.db import get_db
    from tools.radar_sync.react_tracer import ReActTracer

    db     = get_db()
    client = GeminiLLMClient(api_key=GOOGLE_API_KEY, model=_TRACKER_MODEL)
    tracer = ReActTracer(cpmrn, encounter, step="problem_tracker", db=db)

    problems: list[dict] = structured_summary.get("problems", [])
    if not problems:
        logger.info("problem_tracker: no problems in summary for %s enc=%d", cpmrn, encounter)
        return {"problem_tracker": "no_problems"}

    # Build the stored state summary for the model
    stored_names = [
        d["problem_name"]
        for d in db["patient_problems"].find(
            {"CPMRN": cpmrn, "encounter": encounter},
            {"problem_name": 1},
        )
    ]
    new_names = [p["name"] for p in problems if p["name"] not in stored_names]

    problem_block = "\n".join(
        f"  {i+1}. {p['name']} [{p.get('status','?').upper()}]\n"
        f"     Current state: {p.get('current_state', 'not specified')}\n"
        f"     Management: {p.get('management', 'not specified')}"
        for i, p in enumerate(problems)
    )

    new_block = (
        f"\nNEW problems (no prior state): {', '.join(new_names)}"
        if new_names else "\nAll problems have prior state — check stored state with get_problem_state."
    )

    user_msg = (
        f"Patient: {cpmrn} (encounter {encounter})\n"
        f"Snapshot time: {snapshot_at.strftime('%Y-%m-%d %H:%M UTC')}\n\n"
        f"Current problems:\n{problem_block}\n{new_block}\n\n"
        f"Review each problem using the available tools and call set_all_assessments when done."
    )

    messages: list[dict] = [{"role": "user", "content": user_msg}]
    final_assessments: list[dict] = []

    for round_num in range(_MAX_TOOL_ROUNDS):
        tracer.start_round(round_num)
        try:
            force = round_num < _MAX_TOOL_ROUNDS - 1
            resp = client.create_message(
                messages=messages,
                tools=_TOOLS,
                system=_SYSTEM,
                max_tokens=6144,
                force_tool=force,
                thinking_budget=_THINKING_BUDGET,
            )
        except Exception:
            logger.exception("problem_tracker: LLM call failed on round %d for %s", round_num, cpmrn)
            tracer.end_round()
            break

        tracer.log_tokens(resp.usage.input_tokens, resp.usage.output_tokens)

        assistant_parts: list[dict] = []
        tool_calls: list[dict] = []

        for block in resp.content:
            if block.type == "thinking":
                tracer.log_thinking(block.text)
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

        tool_results: list[dict] = []
        for tc in tool_calls:
            if tc.name == "set_all_assessments":
                final_assessments = tc.input.get("assessments", [])
                logger.info(
                    "problem_tracker: assessments for %s: %s",
                    cpmrn,
                    [(a["problem_name"], a.get("clinical_status"), a.get("should_alert")) for a in final_assessments],
                )
                tool_results.append({
                    "type": "tool_result", "name": tc.name, "id": tc.id,
                    "content": "Assessments recorded.",
                })
            else:
                result_text = _run_tool(tc.name, tc.input, cpmrn, encounter, db)
                tracer.log_tool_result(tc.name, result_text)
                tool_results.append({
                    "type": "tool_result", "name": tc.name, "id": tc.id,
                    "content": result_text,
                })

        if tool_results:
            messages.append({"role": "user", "content": tool_results})

        if final_assessments:
            break

    # ── Persist + alert ────────────────────────────────────────────────────────
    now = datetime.now(timezone.utc)
    alerts_sent: list[str] = []
    alerts_suppressed: list[str] = []

    if not final_assessments:
        logger.warning("problem_tracker: no assessments returned for %s — skipping upsert", cpmrn)
        tracer.save(final_output={"error": "no_assessments"})
        return {"problem_tracker": "no_assessments"}

    for assessment in final_assessments:
        problem_name = assessment.get("problem_name", "")
        should_alert = assessment.get("should_alert", False)
        alerted = False

        if should_alert:
            if _should_suppress_alert(cpmrn, encounter, problem_name, db):
                alerts_suppressed.append(problem_name)
                logger.info(
                    "problem_tracker: alert suppressed for '%s' %s enc=%d (cooldown)",
                    problem_name, cpmrn, encounter,
                )
                should_alert = False
            else:
                # Fire Google Chat alert
                try:
                    from backend.services.emr.db import get_db as _get_db
                    from tools.radar_sync.gchat_notifier import send_gchat_alert
                    cfg = db["app_settings"].find_one({"_id": "gchat_webhook"})
                    if cfg and cfg.get("enabled") and cfg.get("url"):
                        fake_summary = {
                            "problems": [
                                {
                                    "name":          problem_name,
                                    "status":        assessment.get("clinical_status", "worsening"),
                                    "current_state": assessment.get("alert_reason", ""),
                                }
                            ],
                            "narrative": structured_summary.get("narrative", ""),
                        }
                        cds_result = {"immediate_next_steps": assessment.get("suggestions", [])}
                        sent = send_gchat_alert(
                            cpmrn, encounter, [problem_name],
                            fake_summary, cds_result, cfg["url"],
                        )
                        if sent:
                            alerts_sent.append(problem_name)
                            alerted = True
                            logger.info("problem_tracker: alert sent for '%s' %s enc=%d", problem_name, cpmrn, encounter)
                except Exception:
                    logger.exception("problem_tracker: gchat alert failed for '%s' %s enc=%d", problem_name, cpmrn, encounter)

        _upsert_problem(cpmrn, encounter, assessment, now, alerted, db)

    tracer.save(final_output={
        "assessments": [(a["problem_name"], a.get("clinical_status"), a.get("should_alert")) for a in final_assessments],
        "alerts_sent":       alerts_sent,
        "alerts_suppressed": alerts_suppressed,
    })

    return {
        "problem_tracker": "ok",
        "problems_assessed": len(final_assessments),
        "alerts_sent":       alerts_sent,
        "alerts_suppressed": alerts_suppressed,
    }
