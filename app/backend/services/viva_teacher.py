"""
Teacher agent for the clinical Viva loop.

generate_trajectory()    — builds the examiner's private case arc (once per session)
generate_first_scenario() — produces the opening scenario for turn 1
generate_next_turn()      — advances the case after each student answer
"""

import json
import logging
import re

from .llm_client import get_llm_client

log = logging.getLogger("wiki.viva_teacher")

_TRAJECTORY_SYSTEM = (
    "You are an experienced clinical educator designing a Viva voce examination. "
    "Write concise examiner notes. No patient-facing prose."
)

_TRAJECTORY_PROMPT = """\
Design a case trajectory for a clinical Viva on: "{topic}"

Write 200-300 words of examiner notes covering:
- Opening presentation (what the student sees first)
- Correct management path and how the case evolves if managed well
- Key complications or branch points if critical steps are missed
- How the case should conclude (stabilisation, safe discharge, or irreversible deterioration)

These are private examiner notes — not a patient summary. Be specific about
clinical values, drug doses, and decision thresholds.
"""

_TEACHER_SYSTEM = (
    "You are a senior ICU clinician conducting a clinical Viva examination. "
    "Advance the case based on the student's answer. "
    "Output ONLY valid JSON — no markdown fences, no prose outside the JSON."
)

_FIRST_SCENARIO_PROMPT = """\
EXAMINER TRAJECTORY (private):
{trajectory}

Generate the OPENING scenario for turn 1 of this viva — the initial presentation.

Output ONLY valid JSON with these exact keys:
{{
  "clinical_context": "2-3 sentence patient presentation",
  "question": "Specific focused question about the most appropriate next action",
  "phase": "EVOLVING",
  "difficulty": "MEDIUM",
  "csv_content": "<CSV — see rules below>"
}}

csv_content rules:
- Header: timestamp_ist,event_category,event_type,actor_name,actor_role,summary
- 8-10 data rows, timestamps starting at 2024-01-15 08:00:00 in IST format
- event_category: LAB | VITAL | TASK | CHAT
- Build a coherent clinical narrative leading to the question
- Quote any summary field containing a comma
"""

_NEXT_TURN_PROMPT = """\
EXAMINER TRAJECTORY (private):
{trajectory}

TURNS SO FAR:
{history}

STUDENT'S LAST ANSWER (Turn {turn_num}):
{last_answer}

This is turn {turn_num} of maximum {max_turns}.

Rules:
- If the student acted appropriately → advance the case naturally (improvement, new labs, next phase)
- If they missed something critical → surface a realistic consequence
- If turn >= {near_end} OR the case has reached a natural conclusion → signal completion
- Keep clinical_context to 2-3 sentences describing what happened since the last question
- The csv_content MUST add new timeline rows reflecting events AFTER the student's last intervention
- New timestamps should continue from where the previous scenario ended
- Add 4-6 new rows only (do not repeat prior rows)

Output ONE of:

A) Next scenario JSON:
{{"clinical_context": "...", "question": "...", "phase": "EVOLVING|ESCALATION|DETERIORATION|MANAGEMENT|LATE", "difficulty": "EASY|MEDIUM|HARD", "csv_content": "timestamp_ist,...\\nrow1\\nrow2..."}}

B) Case complete JSON:
{{"complete": true, "outcome": "One sentence describing the case outcome."}}
"""


def generate_trajectory(topic: str, model: str | None = None) -> str:
    """Generate the private examiner trajectory for a viva topic."""
    llm = get_llm_client(model=model)
    prompt = _TRAJECTORY_PROMPT.format(topic=topic)
    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_TRAJECTORY_SYSTEM,
        max_tokens=1000,
        force_tool=False,
        thinking_budget=0,
    )
    text = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    log.info("Generated trajectory for topic=%r (%d chars)", topic, len(text))
    return text


def generate_first_scenario(topic: str, trajectory: str, model: str | None = None) -> dict:
    """Generate the opening scenario for turn 1."""
    llm = get_llm_client(model=model)
    prompt = _FIRST_SCENARIO_PROMPT.format(trajectory=trajectory)
    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_TEACHER_SYSTEM,
        max_tokens=2000,
        force_tool=False,
        thinking_budget=0,
    )
    raw = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("First scenario parse error: %s\n%s", exc, raw[:300])
        raise ValueError(f"Teacher returned invalid JSON for first scenario: {exc}") from exc
    log.info("Generated first scenario for topic=%r", topic)
    return data


def generate_next_turn(
    trajectory: str,
    turns_so_far: list[dict],
    last_answer_text: str,
    turn_num: int,
    max_turns: int,
    model: str | None = None,
) -> dict:
    """
    Advance the case after the student's answer.

    Returns one of:
      {"complete": False, "scenario": {clinical_context, question, phase, difficulty, csv_content}}
      {"complete": True,  "outcome": "..."}
    """
    llm = get_llm_client(model=model)
    history = _format_history(turns_so_far)
    near_end = max_turns - 1

    prompt = _NEXT_TURN_PROMPT.format(
        trajectory=trajectory,
        history=history,
        last_answer=last_answer_text[:1500],
        turn_num=turn_num,
        max_turns=max_turns,
        near_end=near_end,
    )

    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_TEACHER_SYSTEM,
        max_tokens=2000,
        force_tool=False,
        thinking_budget=0,
    )
    raw = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    raw = re.sub(r"^```(?:json)?\s*", "", raw.strip())
    raw = re.sub(r"\s*```$", "", raw.strip())

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.warning("Teacher next-turn parse error (turn %d): %s\n%s", turn_num, exc, raw[:300])
        return {"complete": True, "outcome": "Case concluded (teacher parse error)."}

    if data.get("complete"):
        return {"complete": True, "outcome": data.get("outcome", "Case concluded.")}

    return {"complete": False, "scenario": data}


def _format_history(turns: list[dict]) -> str:
    if not turns:
        return "(none — this is turn 1)"
    lines = []
    for t in turns:
        num = t.get("turn_num", "?")
        ctx = t.get("scenario", {}).get("clinical_context", "")
        q = t.get("scenario", {}).get("question", "")
        ans = t.get("student_answer_text", "")
        lines.append(f"--- Turn {num} ---")
        lines.append(f"Context: {ctx}")
        lines.append(f"Question: {q}")
        lines.append(f"Student: {ans[:600]}")
    return "\n".join(lines)
