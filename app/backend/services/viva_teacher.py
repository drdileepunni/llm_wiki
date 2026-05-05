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
  "clinical_context": "2-3 sentence patient presentation summary (for display only)",
  "question": "Specific focused clinical question about the most appropriate next action",
  "phase": "EVOLVING",
  "difficulty": "MEDIUM",
  "patient_state": {{ ... see schema below ... }}
}}

patient_state schema — populate what is relevant to the scenario:
{{
  "vitals": {{
    "BP": "<systolic/diastolic string, e.g. 88/54>",
    "HR": <number>,
    "SpO2": <number, percent>,
    "RR": <number>,
    "Temperature": <number, Celsius>,
    "TherapyDevice": "<room air | oxygen mask | Venturi mask | HFNC | NIV | ventilator>",
    "FiO2": <0.21–1.0>,
    "OxygenFlow": <L/min, optional>,
    "AVPU": "<A | V | P | U>"
  }},
  "labs": [
    {{
      "name": "<test name, e.g. VBG>",
      "attributes": {{
        "<key>": {{"name": "<display name>", "value": "<value as string>", "errorRange": {{"min": <n>, "max": <n>}}}}
      }}
    }}
  ],
  "notes": [
    {{"category": "event", "text": "<concise note, e.g. ECG: sinus tachycardia at 112 bpm, no ST changes>"}},
    {{"category": "nursing", "text": "<nursing observation>"}}
  ],
  "io": {{
    "urine_ml": <number>,
    "intake_ml": <number>,
    "period_mins": <number>
  }},
  "vent_flags": {{
    "isNIV": <true|false>,
    "isHFNC": <true|false>,
    "isIntubated": <true|false>
  }}
}}

Rules:
- Include vitals always — they are the core of the presentation
- Include labs only if results are available at presentation (e.g. VBG, troponin already drawn)
- Include notes for any significant event findings (ECG, imaging, nursing observations)
- Include io only if relevant (e.g. oliguria is part of the presentation)
- Include vent_flags only if patient is on respiratory support at presentation
- Be physiologically consistent — values must match the clinical narrative
"""

_NEXT_TURN_PROMPT = """\
EXAMINER TRAJECTORY (private):
{trajectory}

TURNS SO FAR:
{history}

STUDENT'S LAST ANSWER (Turn {turn_num}):
{last_answer}

SIMULATION RESULTS (vitals, labs, IO already written to the patient chart):
{simulation_summary}

This is turn {turn_num} of maximum {max_turns}.

Rules:
- The simulation has already updated the patient's vitals, labs, and IO in the chart
- Base clinical_context on the SIMULATION RESULTS — reference actual values ("BP is now 120/78", "VBG pH 7.34")
- If the student acted appropriately → advance the case naturally toward trajectory resolution
- If they missed something critical → surface realistic consequences from the simulation
- If turn >= {near_end} OR the case has reached a natural conclusion → signal completion
- Keep clinical_context to 2-3 sentences
- Use additional_notes ONLY for supplementary narrative events not captured by the simulator
  (e.g. consultant review, family discussion, imaging result, ward observation)
  Leave additional_notes as an empty list [] if nothing to add

Output ONE of:

A) Next scenario JSON:
{{
  "clinical_context": "2-3 sentence summary referencing actual simulation values",
  "question": "Specific focused clinical question",
  "phase": "EVOLVING|ESCALATION|DETERIORATION|MANAGEMENT|LATE",
  "difficulty": "EASY|MEDIUM|HARD",
  "additional_notes": [
    {{"category": "event", "text": "..."}}
  ]
}}

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
    simulation_summary: str = "",
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
        simulation_summary=simulation_summary or "(not available)",
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
        sim = t.get("simulation_summary", "")
        lines.append(f"--- Turn {num} ---")
        lines.append(f"Context: {ctx}")
        lines.append(f"Question: {q}")
        lines.append(f"Student: {ans[:600]}")
        if sim:
            lines.append(f"Simulation results: {sim}")
    return "\n".join(lines)
