"""
Viva clinical simulator.

After the student's orders are placed, this agent generates realistic clinical
responses (vitals changes, lab results, ECG notes, I/O) and writes them
directly to the dummy patient's MongoDB document.

It runs as a separate LLM call so the teacher can reference actual values
("VBG pH is now 7.34") rather than inventing them, and lab orders are
automatically completed so the agent won't re-order them next turn.

write_patient_state() is also used by viva.py to seed the initial patient
state produced by the teacher's first scenario.
"""

from __future__ import annotations

import json
import logging
import re

from .llm_client import get_llm_client
from .emr import (
    VIVA_DUMMY_CPMRN,
    get_latest_vitals,
    push_vitals,
    push_lab_result,
    push_event_note,
    push_io_entry,
    complete_lab_orders,
    update_vent_flags,
)

log = logging.getLogger("wiki.viva_sim")

_SYSTEM = (
    "You are a clinical simulation engine for a medical training viva. "
    "Generate realistic patient responses to clinical interventions. "
    "Output ONLY valid JSON — no markdown, no prose."
)

_PROMPT = """\
CASE TRAJECTORY (private — defines expected patient evolution):
{trajectory}

CURRENT PATIENT STATE (before these orders took effect):
{current_state}

ORDERS PLACED THIS TURN:
{orders_text}

CLINICAL CONTEXT THIS TURN:
{clinical_context}

Turn {turn_num} of {max_turns}. Approximately 30-60 minutes of simulated time has passed.

Your job: generate the patient's realistic response to these interventions.
Follow the trajectory's expected evolution if orders were appropriate.
If critical orders were missed, surface realistic consequences.

Return ONLY this JSON structure (omit any key that is not applicable):

{{
  "vitals": {{
    "BP": "<systolic/diastolic as string, e.g. 145/90>",
    "HR": <number>,
    "SpO2": <number, percent>,
    "RR": <number>,
    "Temperature": <number, Celsius>,
    "TherapyDevice": "<room air | oxygen mask | NIV | HFNC | ventilator | none>",
    "FiO2": <0.21–1.0>,
    "OxygenFlow": <L/min if applicable>,
    "VentMode": "<if intubated>",
    "VentPEEP": <if intubated>,
    "AVPU": "<A | V | P | U>"
  }},
  "labs": [
    {{
      "name": "<test name exactly matching the order, e.g. VBG>",
      "attributes": {{
        "<param_key>": {{
          "name": "<display name>",
          "value": "<value as string>",
          "errorRange": {{"min": <normal_min>, "max": <normal_max>}}
        }}
      }}
    }}
  ],
  "notes": [
    {{"category": "event", "text": "<concise clinical note, e.g. ECG: sinus tachycardia at 96 bpm, no ST changes>"}}
  ],
  "io": {{
    "urine_ml": <number>,
    "intake_ml": <number>,
    "period_mins": 60
  }},
  "complete_lab_orders": ["<lab names to mark as resulted and remove from active orders>"],
  "vent_flags": {{
    "isNIV": <true|false>,
    "isHFNC": <true|false>,
    "isIntubated": <true|false>
  }}
}}

Rules:
- Only include "labs" if a lab test was ordered this turn
- Only include "notes" if a monitoring order (ECG, arterial line, etc.) was placed
- Only include "io" if urine output monitoring was ordered
- Only include "vent_flags" if a respiratory support order (NIV, HFNC, intubation) was placed
- "complete_lab_orders" must match the names in the active lab orders (VBG, ABG, etc.)
- Values must be physiologically realistic for the clinical scenario
"""


def write_patient_state(state: dict, cpmrn: str) -> str:
    """
    Write a patient_state dict to MongoDB.  Handles any subset of keys:
    vitals, labs, notes, io, complete_lab_orders, vent_flags.
    Returns a plain-text summary of what was written.
    """
    summary_parts: list[str] = []

    if state.get("vitals"):
        push_vitals(cpmrn, state["vitals"])
        v = state["vitals"]
        vital_str = "  ".join(
            f"{k}: {v[k]}" for k in ("BP", "HR", "SpO2", "RR", "TherapyDevice", "FiO2")
            if k in v
        )
        summary_parts.append(f"Vitals: {vital_str}")

    for lab in state.get("labs", []):
        push_lab_result(cpmrn, lab["name"], lab.get("attributes", {}))
        attrs = lab.get("attributes", {})
        vals = ", ".join(
            f"{v.get('name', k)}: {v.get('value', '?')}"
            for k, v in attrs.items()
            if isinstance(v, dict)
        )
        summary_parts.append(f"{lab['name']}: {vals}")

    for note in state.get("notes", []):
        push_event_note(cpmrn, note.get("category", "event"), note["text"])
        summary_parts.append(note["text"])

    if state.get("io"):
        io = state["io"]
        push_io_entry(
            cpmrn,
            urine_ml=io.get("urine_ml", 0),
            intake_ml=io.get("intake_ml", 0),
            period_mins=io.get("period_mins", 60),
        )
        summary_parts.append(
            f"Urine output: {io.get('urine_ml', '?')} mL over {io.get('period_mins', 60)} min"
        )

    if state.get("complete_lab_orders"):
        complete_lab_orders(cpmrn, state["complete_lab_orders"])

    if state.get("vent_flags"):
        vf = state["vent_flags"]
        update_vent_flags(
            cpmrn,
            is_niv=vf.get("isNIV"),
            is_hfnc=vf.get("isHFNC"),
            is_intubated=vf.get("isIntubated"),
        )

    return "\n".join(summary_parts) if summary_parts else "(no state data)"


def _orders_to_text(orders: list[dict]) -> str:
    if not orders:
        return "(none)"
    lines = []
    for o in orders:
        t = o.get("order_type", "")
        name = o.get("orderable_name") or o.get("recommendation") or "—"
        details = o.get("order_details") or {}
        instr = details.get("instructions") or o.get("notes") or ""
        line = f"[{t.upper()}] {name}"
        if instr:
            line += f" — {instr}"
        lines.append(line)
    return "\n".join(lines)


def _current_state_text(cpmrn: str) -> str:
    try:
        v = get_latest_vitals(cpmrn)
        if v.get("vitals") is None and "BP" not in v:
            return "(no prior vitals recorded)"
        parts = []
        for label, key in [
            ("BP", "BP"), ("HR", "HR"), ("SpO2", "SpO2"), ("RR", "RR"),
            ("Temp", "Temperature"), ("Device", "TherapyDevice"), ("FiO2", "FiO2"),
        ]:
            val = v.get(key)
            if val is not None:
                parts.append(f"{label}: {val}")
        return "  ".join(parts) if parts else "(no prior vitals)"
    except Exception as exc:
        log.warning("Could not read current vitals for simulation: %s", exc)
        return "(vitals unavailable)"


def simulate_and_write(
    trajectory: str,
    orders: list[dict],
    cpmrn: str,
    clinical_context: str,
    turn_num: int,
    max_turns: int,
    model: str | None = None,
) -> dict:
    """
    Run the simulator LLM, write results to MongoDB, and return a
    plain-text summary of what happened (for the teacher to reference).

    Returns a dict with keys: summary (str), raw (dict).
    """
    llm = get_llm_client(model=model)
    current_state = _current_state_text(cpmrn)
    orders_text = _orders_to_text(orders)

    prompt = _PROMPT.format(
        trajectory=trajectory,
        current_state=current_state,
        orders_text=orders_text,
        clinical_context=clinical_context,
        turn_num=turn_num,
        max_turns=max_turns,
    )

    response = llm.create_message(
        messages=[{"role": "user", "content": prompt}],
        tools=[],
        system=_SYSTEM,
        max_tokens=1500,
        force_tool=False,
    )

    raw_text = next((b.text for b in response.content if hasattr(b, "text") and b.text), "")
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text.strip())
    raw_text = re.sub(r"\s*```$", "", raw_text.strip())

    try:
        sim = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        log.warning("Simulator JSON parse error: %s\n%s", exc, raw_text[:300])
        return {"summary": "(simulation unavailable — parse error)", "raw": {}}

    log.info(
        "Simulator turn %d: vitals=%s  labs=%d  notes=%d  io=%s  complete_labs=%s",
        turn_num,
        sim.get("vitals", {}).get("BP", "—"),
        len(sim.get("labs", [])),
        len(sim.get("notes", [])),
        "yes" if sim.get("io") else "no",
        sim.get("complete_lab_orders", []),
    )

    # ── Write to MongoDB and build summary ────────────────────────────────────
    summary = write_patient_state(sim, cpmrn)
    return {"summary": summary or "(no simulation data)", "raw": sim}
